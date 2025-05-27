import kopf
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import base64
import asyncio
from kubernetes.utils import create_from_dict
import os
import logging

# Load Kubernetes config (in-cluster or local)
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_v1 = client.CoreV1Api()

# --- Utility Functions ---

def create_or_update_secret(name, namespace, data_dict):
    """
    Create or update a Kubernetes Secret with the given data.
    """
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data={k: base64.b64encode(v.encode()).decode() for k, v in data_dict.items()},
        type="Opaque"
    )
    try:
        core_v1.read_namespaced_secret(name, namespace)
        core_v1.replace_namespaced_secret(name, namespace, secret)
    except ApiException as e:
        if e.status == 404:
            core_v1.create_namespaced_secret(namespace, secret)
        else:
            raise

def get_expected_yaml_from_secret(namespace, secret_name, key):
    """
    Retrieve and decode expected YAML from a Secret.
    """
    secret = core_v1.read_namespaced_secret(secret_name, namespace)
    data = secret.data or {}
    encoded = data.get(key)
    if not encoded:
        raise ValueError(f"Secret {secret_name} does not have key {key}")
    decoded = base64.b64decode(encoded).decode()
    return decoded

def load_manifests(yaml_str):
    """
    Parses a YAML string into a list of resource dicts.
    Handles multi-document YAML separated by '---'.
    """
    docs = list(yaml.safe_load_all(yaml_str))
    return [doc for doc in docs if doc is not None]

async def apply_manifest(manifest, namespace):
    kind = manifest.get("kind")
    metadata = manifest.setdefault("metadata", {})
    name = metadata.get("name")
    ns = metadata.setdefault("namespace", namespace)
    api = client.CoreV1Api()

    logging.info(f"Applying {kind} '{name}' in namespace '{ns}'")
    try:
        if kind == "Pod":
            try:
                existing = api.read_namespaced_pod(name, ns).to_dict()
                if not compare_resources(manifest, existing):
                    logging.info(f"Pod '{name}' spec changed, deleting for re-creation.")
                    api.delete_namespaced_pod(name, ns)
                    for _ in range(30):
                        await asyncio.sleep(1)
                        try:
                            api.read_namespaced_pod(name, ns)
                        except ApiException as e:
                            if e.status == 404:
                                logging.info(f"Pod '{name}' deleted, creating new Pod.")
                                api.create_namespaced_pod(ns, manifest)
                                break
                            else:
                                raise
                    else:
                        raise Exception(f"Pod {name} not deleted after waiting")
                else:
                    logging.info(f"Pod '{name}' already matches manifest, skipping.")
            except ApiException as e:
                if e.status == 404:
                    logging.info(f"Pod '{name}' does not exist, creating new Pod.")
                    api.create_namespaced_pod(ns, manifest)
                else:
                    raise
        else:
            # For all other resource kinds, use the generic utility
            logging.info(f"Applying resource kind '{kind}' with generic utility.")
            create_from_dict(client.ApiClient(), manifest, namespace=namespace)
    except Exception as e:
        logging.error(f"Failed to apply {kind} '{name}' in '{ns}': {e}", exc_info=True)
        raise

    await asyncio.sleep(0)

def get_live_resource(kind, api_version, name, namespace):
    """
    Fetches the live resource from the cluster.
    Supports Pod, ConfigMap, and Secret.
    """
    if kind == "Pod":
        return core_v1.read_namespaced_pod(name, namespace).to_dict()
    elif kind == "ConfigMap":
        return core_v1.read_namespaced_config_map(name, namespace).to_dict()
    elif kind == "Secret":
        return core_v1.read_namespaced_secret(name, namespace).to_dict()
    else:
        raise NotImplementedError(f"get_live_resource does not support kind: {kind}")

def is_subset(expected, live):
    """
    Recursively checks if all fields in expected are present and equal in live.
    """
    if isinstance(expected, dict) and isinstance(live, dict):
        for k, v in expected.items():
            if k not in live or not is_subset(v, live[k]):
                return False
        return True
    elif isinstance(expected, list) and isinstance(live, list):
        if len(expected) != len(live):
            return False
        return all(is_subset(e, l) for e, l in zip(expected, live))
    else:
        return expected == live

def compare_resources(expected, live):
    """
    Compares two Kubernetes resource dicts for equality, focusing on relevant fields.
    Ignores fields commonly mutated by the API server.
    """
    kind = expected.get("kind")
    def filter_metadata(meta):
        return {
            "name": meta.get("name"),
            "namespace": meta.get("namespace"),
        }

    if kind in ("ConfigMap", "Secret"):
        return (
            expected.get("kind") == live.get("kind") and
            expected.get("apiVersion") == live.get("apiVersion") and
            filter_metadata(expected.get("metadata", {})) == filter_metadata(live.get("metadata", {})) and
            is_subset(expected.get("data", {}), live.get("data", {})) and
            (expected.get("type") == live.get("type") if kind == "Secret" else True)
        )
    elif kind == "Pod":
        expected_spec = expected.get("spec", {})
        live_spec = live.get("spec", {})
        return (
            expected.get("kind") == live.get("kind") and
            filter_metadata(expected.get("metadata", {})) == filter_metadata(live.get("metadata", {})) and
            is_subset(expected_spec.get("containers", []), live_spec.get("containers", []))
        )
    else:
        return (
            expected.get("kind") == live.get("kind") and
            expected.get("apiVersion") == live.get("apiVersion") and
            filter_metadata(expected.get("metadata", {})) == filter_metadata(live.get("metadata", {})) and
            is_subset(expected.get("spec", {}), live.get("spec", {}))
        )

# --- Operator Handlers ---

@kopf.on.create('training.dev', 'v1', 'labs')
async def create_lab(spec, patch, name, namespace, body, **kwargs):
    """
    Handles creation of Lab resources.
    Applies manifests to create resources in the cluster.
    """
    given_yaml = spec.get("given")
    given_file = spec.get("givenFile")

    # --- Handle given manifest ---
    if not given_yaml and given_file:
        try:
            with open(given_file, "r") as f:
                given_yaml = f.read()
        except Exception as e:
            logging.error(f"Failed to read givenFile: {e}", exc_info=True)
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to read givenFile: {e}"
            return

    if not given_yaml:
        patch.status["ready"] = False
        patch.status["error"] = "'given' or 'givenFile' field is required"
        return

    try:
        given_docs = load_manifests(given_yaml)
    except Exception as e:
        logging.error(f"Failed to parse manifests: {e}", exc_info=True)
        patch.status["ready"] = False
        patch.status["error"] = f"Failed to parse manifests: {e}"
        return

    # Only create resources here
    for manifest in given_docs:
        try:
            await apply_manifest(manifest, namespace)
        except Exception as e:
            logging.error(f"Failed to apply given manifest: {e}", exc_info=True)
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to apply given manifest: {e}"
            return

    # Then validate as usual
    await validate_lab(spec, patch, name, namespace, body)

@kopf.on.update('training.dev', 'v1', 'labs')
@kopf.timer('training.dev', 'v1', 'labs', interval=5.0)
async def validate_lab(spec, patch, name, namespace, body, expected_docs=None, **kwargs):
    """
    Validates Lab resources against the expected state.
    Updates status and generates events based on the validation result.
    """
    expected = spec.get("expected")
    expected_ref = spec.get("expectedRef")
    expected_file = spec.get("expectedFile")

    # --- Handle expected manifest ---
    expected_yaml = None
    if expected and not expected_ref:
        secret_name = f"lab-{name}-expected"
        create_or_update_secret(secret_name, namespace, {"expected.yaml": expected})

        patch.spec["expected"] = None
        patch.spec["expectedRef"] = {
            "secretName": secret_name,
            "key": "expected.yaml"
        }

        kopf.event(
            body,
            type="Normal",
            reason="Initializing",
            message="Please wait, the Lab is initializing and preparing resources."
        )

        expected_yaml = expected
    elif expected_ref:
        secret_name = expected_ref.get("secretName")
        key = expected_ref.get("key", "expected.yaml")
        expected_yaml = get_expected_yaml_from_secret(namespace, secret_name, key)
    elif expected_file:
        try:
            with open(expected_file, "r") as f:
                expected_yaml = f.read()
        except Exception as e:
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to read expectedFile: {e}"
            return
    else:
        patch.status["ready"] = False
        patch.status["error"] = "Expected manifests not defined in spec.expected, spec.expectedFile, or spec.expectedRef"
        return

    if expected_yaml:
        try:
            expected_docs = load_manifests(expected_yaml)
        except Exception as e:
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to parse expected manifests: {e}"
            return
    else:
        expected_docs = []

    # Validate expected manifests against live cluster
    all_match = True
    for expected in expected_docs:
        try:
            live = get_live_resource(
                expected["kind"],
                expected["apiVersion"],
                expected["metadata"]["name"],
                expected["metadata"].get("namespace", namespace)
            )
        except ApiException as e:
            if e.status == 404:
                patch.status["ready"] = False
                patch.status["error"] = (
                    f"Resource {expected['kind']}/{expected['metadata']['name']} not found in namespace "
                    f"{expected['metadata'].get('namespace', namespace)}"
                )
                return
            else:
                patch.status["ready"] = False
                patch.status["error"] = f"Failed to get live resource: {e.reason}"
                return
        except Exception as e:
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to get live resource: {str(e)}"
            return

        if not compare_resources(expected, live):
            all_match = False
            break

    patch.status["ready"] = all_match
    if all_match:
        patch.status["message"] = "✅ The Lab is successfully fixed. Well done!"
        patch.status["error"] = None
    else:
        patch.status["message"] = None
        patch.status["error"] = (
            "❌ The Lab is not fixed yet.\n"
            "Please ensure all resources match the expected configuration."
        )

    last_ready = body.get("status", {}).get("ready")
    if all_match and last_ready is not True:
        kopf.event(
            body,
            type="Normal",
            reason="LabFixed",
            message="The Lab is successfully fixed."
        )
    elif not all_match and last_ready is not False:
        kopf.event(
            body,
            type="Warning",
            reason="LabNotFixed",
            message="The cluster state does not match the expected manifests. Keep looking for the issue."
        )

@kopf.on.delete('training.dev', 'v1', 'labs')
async def delete_lab(spec, name, namespace, **kwargs):
    """
    Deletes all resources defined in 'given' and 'expected' when the Lab is deleted.
    Also deletes the operator-created expected Secret.
    """
    manifests = []
    given_yaml = spec.get("given")
    expected = spec.get("expected")
    expected_ref = spec.get("expectedRef")

    # Collect manifests from 'given'
    if given_yaml:
        try:
            manifests.extend(load_manifests(given_yaml))
        except Exception:
            pass

    # Collect manifests from 'expected' or 'expectedRef'
    expected_yaml = None
    if expected and not expected_ref:
        expected_yaml = expected
    elif expected_ref:
        secret_name = expected_ref.get("secretName")
        key = expected_ref.get("key", "expected.yaml")
        try:
            expected_yaml = get_expected_yaml_from_secret(namespace, secret_name, key)
        except Exception:
            pass

    if expected_yaml:
        try:
            manifests.extend(load_manifests(expected_yaml))
        except Exception:
            pass

    # Delete each resource (ignore 404 errors)
    for manifest in manifests:
        kind = manifest.get("kind")
        metadata = manifest.get("metadata", {})
        res_name = metadata.get("name")
        res_ns = metadata.get("namespace", namespace)
        try:
            logging.info(f"Deleting {kind} '{res_name}' in namespace '{res_ns}'")
            if kind == "Pod":
                core_v1.delete_namespaced_pod(res_name, res_ns)
            elif kind == "ConfigMap":
                core_v1.delete_namespaced_config_map(res_name, res_ns)
            elif kind == "Secret":
                core_v1.delete_namespaced_secret(res_name, res_ns)
            # Add more kinds as needed
        except ApiException as e:
            if e.status != 404:
                logging.error(f"Failed to delete {kind} '{res_name}' in '{res_ns}': {e}")
                raise

    # Delete the lab's expected secret if it exists
    secret_name = f"lab-{name}-expected"
    try:
        logging.info(f"Deleting expected Secret '{secret_name}' in namespace '{namespace}'")
        core_v1.delete_namespaced_secret(secret_name, namespace)
    except ApiException as e:
        if e.status != 404:
            logging.error(f"Failed to delete expected Secret '{secret_name}' in '{namespace}': {e}")
            raise
