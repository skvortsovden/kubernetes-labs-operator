import kopf
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import base64
import asyncio
from kubernetes.utils import create_from_dict

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
    """
    Apply a manifest to the cluster. Supports Pod, ConfigMap, Secret.
    For Pods, deletes and recreates if already exists (immutable fields).
    """
    kind = manifest.get("kind")
    metadata = manifest.setdefault("metadata", {})
    name = metadata.get("name")
    ns = metadata.setdefault("namespace", namespace)
    api = client.CoreV1Api()

    try:
        if kind == "Pod":
            try:
                api.read_namespaced_pod(name, ns)
                api.delete_namespaced_pod(name, ns)
                # Wait for pod deletion to complete
                for _ in range(30):
                    await asyncio.sleep(1)
                    try:
                        api.read_namespaced_pod(name, ns)
                    except ApiException as e:
                        if e.status == 404:
                            api.create_namespaced_pod(ns, manifest)
                            break
                        else:
                            raise
                else:
                    raise Exception(f"Pod {name} not deleted after waiting")
            except ApiException as e:
                if e.status == 404:
                    api.create_namespaced_pod(ns, manifest)
                else:
                    raise
        elif kind == "ConfigMap":
            try:
                api.read_namespaced_config_map(name, ns)
                api.replace_namespaced_config_map(name, ns, manifest)
            except ApiException as e:
                if e.status == 404:
                    api.create_namespaced_config_map(ns, manifest)
                else:
                    raise
        elif kind == "Secret":
            try:
                api.read_namespaced_secret(name, ns)
                api.replace_namespaced_secret(name, ns, manifest)
            except ApiException as e:
                if e.status == 404:
                    api.create_namespaced_secret(ns, manifest)
                else:
                    raise
        else:
            # For other resource kinds, use the generic utility
            create_from_dict(client.ApiClient(), manifest, namespace=namespace)
    except Exception as e:
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
@kopf.on.update('training.dev', 'v1', 'labs')
async def reconcile(spec, patch, name, namespace, body, **kwargs):
    """
    Main reconciliation handler for Lab resources.
    Handles creation/update, applies manifests, and checks cluster state.
    """
    expected = spec.get("expected")
    expected_ref = spec.get("expectedRef")

    # If plain YAML is provided, convert to Secret and patch Lab to use expectedRef
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
    else:
        patch.status["ready"] = False
        patch.status["error"] = "Expected manifests not defined in spec.expected or spec.expectedRef"
        return

    # Parse and apply 'given' manifests
    given_yaml = spec.get("given")
    if not given_yaml:
        patch.status["ready"] = False
        patch.status["error"] = "'given' field is required"
        return

    try:
        given_docs = load_manifests(given_yaml)
        expected_docs = load_manifests(expected_yaml)
    except Exception as e:
        patch.status["ready"] = False
        patch.status["error"] = f"Failed to parse manifests: {e}"
        return

    for manifest in given_docs:
        try:
            await apply_manifest(manifest, namespace)
        except Exception as e:
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to apply given manifest: {e}"
            return

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
        except Exception as e:
            patch.status["ready"] = False
            patch.status["error"] = f"Failed to get live resource: {e}"
            return

        if not compare_resources(expected, live):
            all_match = False
            break

    patch.status["ready"] = all_match
    if all_match:
        patch.status["message"] = "✅ The Lab is successfully fixed. Well done!"
        patch.status.pop("error", None)
        kopf.event(
            body,
            type="Normal",
            reason="LabFixed",
            message="The Lab is successfully fixed."
        )
    else:
        patch.status.pop("message", None)
        patch.status["error"] = (
            "❌ The Lab is not fixed yet.\n"
            "Please ensure all resources match the expected configuration."
        )
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
            if kind == "Pod":
                core_v1.delete_namespaced_pod(res_name, res_ns)
            elif kind == "ConfigMap":
                core_v1.delete_namespaced_config_map(res_name, res_ns)
            elif kind == "Secret":
                core_v1.delete_namespaced_secret(res_name, res_ns)
            # Add more kinds as needed
        except ApiException as e:
            if e.status != 404:
                raise

    # Delete the lab's expected secret if it exists
    secret_name = f"lab-{name}-expected"
    try:
        core_v1.delete_namespaced_secret(secret_name, namespace)
    except ApiException as e:
        if e.status != 404:
            raise
