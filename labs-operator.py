import kopf
import yaml
from kubernetes import client, config
from kubernetes.client.rest import ApiException
import base64
import asyncio
from kubernetes.utils import create_from_dict
import os
import logging
import re

logging.getLogger("kopf.objects").setLevel(logging.ERROR)

# Load Kubernetes config (in-cluster or local)
try:
    config.load_incluster_config()
except config.ConfigException:
    config.load_kube_config()

core_v1 = client.CoreV1Api()

# --- Utility Functions ---

async def create_or_update_secret(name, namespace, data_dict):
    """
    Create or update a Kubernetes Secret with the given data.
    """
    secret = client.V1Secret(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        data={k: base64.b64encode(v.encode()).decode() for k, v in data_dict.items()},
        type="Opaque"
    )
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, core_v1.read_namespaced_secret, name, namespace)
        await loop.run_in_executor(None, core_v1.replace_namespaced_secret, name, namespace, secret)
    except ApiException as e:
        if e.status == 404:
            await loop.run_in_executor(None, core_v1.create_namespaced_secret, namespace, secret)
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
    Supports Pod, ConfigMap, Secret, PersistentVolumeClaim, and Service.
    """
    if kind == "Pod":
        return dict_keys_to_camel(core_v1.read_namespaced_pod(name, namespace).to_dict())
    elif kind == "ConfigMap":
        return dict_keys_to_camel(core_v1.read_namespaced_config_map(name, namespace).to_dict())
    elif kind == "Secret":
        return dict_keys_to_camel(core_v1.read_namespaced_secret(name, namespace).to_dict())
    elif kind == "PersistentVolumeClaim":
        return dict_keys_to_camel(core_v1.read_namespaced_persistent_volume_claim(name, namespace).to_dict())
    elif kind == "Service":
        return dict_keys_to_camel(core_v1.read_namespaced_service(name, namespace).to_dict())
    else:
        raise NotImplementedError(f"get_live_resource does not support kind: {kind}")

def is_subset(expected, live, path=""):
    """
    Recursively checks if all fields in expected are present and equal in live.
    For lists, checks that each expected element is present somewhere in the live list.
    Logs mismatches for debugging.
    """
    if isinstance(expected, dict) and isinstance(live, dict):
        for k, v in expected.items():
            sub_path = f"{path}.{k}" if path else k
            if k not in live:
                logging.debug(f"[is_subset] Key '{sub_path}' missing in live")
                return False
            if not is_subset(v, live[k], sub_path):
                logging.debug(f"[is_subset] Value mismatch at '{sub_path}': expected={v}, live={live[k]}")
                return False
        return True
    elif isinstance(expected, list) and isinstance(live, list):
        for idx, e in enumerate(expected):
            if not any(is_subset(e, l, f"{path}[{idx}]") for l in live):
                logging.debug(f"[is_subset] List element at '{path}[{idx}]' not found in live")
                return False
        return True
    else:
        if expected != live:
            logging.debug(f"[is_subset] Value mismatch at '{path}': expected={expected}, live={live}")
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

def find_mismatches(expected, live, path=""):
    """
    Recursively find fields present in expected but missing in live.
    Returns a list of property paths that are missing in the live resource.
    Adds debug logging for each comparison step.
    """
    mismatches = []
    if isinstance(expected, dict) and isinstance(live, dict):
        for k, v in expected.items():
            if k == "apiVersion":
                continue  # Ignore apiVersion
            sub_path = f"{path}.{k}" if path else k
            if k not in live:
                logging.debug(f"[find_mismatches] {sub_path} missing in live")
                mismatches.append(f"{sub_path} (missing)")
            else:
                if isinstance(v, (dict, list)) and isinstance(live[k], type(v)):
                    logging.debug(f"[find_mismatches] {sub_path} found, recursing")
                    mismatches.extend(find_mismatches(v, live[k], sub_path))
                else:
                    if v != live[k]:
                        logging.debug(f"[find_mismatches] {sub_path} value mismatch: expected={v}, live={live[k]}")
                        mismatches.append(f"{sub_path} (value mismatch: expected={v}, live={live[k]})")
    elif isinstance(expected, list) and isinstance(live, list):
        if path.endswith("volumeMounts"):
            expected_mounts = {(m.get("mountPath"), m.get("name")) for m in expected}
            live_mounts = {(m.get("mountPath"), m.get("name")) for m in live}
            for mount in expected_mounts:
                if mount not in live_mounts:
                    logging.debug(f"[find_mismatches] {path} missing mount: {mount}")
                    mismatches.append(f"{path} (missing mount: {mount})")
        elif path.endswith("volumes"):
            expected_vols = {v.get("name"): v for v in expected}
            live_vols = {v.get("name"): v for v in live}
            for name, exp_vol in expected_vols.items():
                if name not in live_vols:
                    logging.debug(f"[find_mismatches] {path} missing volume: {name}")
                    mismatches.append(f"{path} (missing volume: {name})")
                else:
                    if "persistentVolumeClaim" in exp_vol:
                        if "persistentVolumeClaim" not in live_vols[name]:
                            logging.debug(f"[find_mismatches] {path}.{name}.persistentVolumeClaim missing in live")
                            mismatches.append(f"{path}.{name}.persistentVolumeClaim (missing)")
        else:
            for i, e in enumerate(expected):
                if i < len(live):
                    logging.debug(f"[find_mismatches] {path}[{i}] recursing")
                    mismatches.extend(find_mismatches(e, live[i], f"{path}[{i}]"))
                else:
                    logging.debug(f"[find_mismatches] {path}[{i}] missing in live")
                    mismatches.append(f"{path}[{i}] (missing)")
    return mismatches

def to_camel_case(s):
    parts = s.split('_')
    return parts[0] + ''.join(word.capitalize() for word in parts[1:])

def dict_keys_to_camel(obj):
    if isinstance(obj, dict):
        new = {}
        for k, v in obj.items():
            new_k = to_camel_case(k)
            new[new_k] = dict_keys_to_camel(v)
        return new
    elif isinstance(obj, list):
        return [dict_keys_to_camel(i) for i in obj]
    else:
        return obj

def is_resource_ready(kind, live):
    """
    Returns True if the resource is considered 'ready' or 'running'.
    Extend this function for more resource kinds as needed.
    """
    if kind == "Pod":
        status = live.get("status", {})
        phase = status.get("phase")
        if phase != "Running":
            return False
        container_statuses = status.get("containerStatuses", [])
        if not container_statuses:
            return False
        for cs in container_statuses:
            # Check if container is ready
            if not cs.get("ready", False):
                return False
            # Check for CrashLoopBackOff or Error
            state = cs.get("state", {})
            if "waiting" in state:
                reason = state["waiting"].get("reason", "")
                if reason in ("CrashLoopBackOff", "Error"):
                    return False
            if "terminated" in state:
                return False
        return True
    elif kind == "Service":
        return True
    elif kind == "ConfigMap":
        return True
    elif kind == "Secret":
        return True
    elif kind == "PersistentVolumeClaim":
        phase = live.get("status", {}).get("phase")
        return phase == "Bound"
    # Add more resource kinds and their readiness logic as needed
    return True  # Default: consider resource ready if it exists

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
    logging.info("validate_lab running for %s", name)
    
    expected = spec.get("expected")
    expected_ref = spec.get("expectedRef")
    expected_file = spec.get("expectedFile")

    # --- Handle expected manifest ---
    expected_yaml = None
    if expected and not expected_ref:
        # Fire Initializing event first
        kopf.event(
            body,
            type="Normal",
            reason="Initializing",
            message="Please wait, the Lab is initializing and preparing resources."
        )

        secret_name = f"lab-{name}-expected"
        await create_or_update_secret(secret_name, namespace, {"expected.yaml": expected})

        patch.spec["expected"] = None
        patch.spec["expectedRef"] = {
            "secretName": secret_name,
            "key": "expected.yaml"
        }

        expected_yaml = expected

        # Fire LabReady event after Secret is created and spec is patched
        kopf.event(
            body,
            type="Normal",
            reason="LabReady",
            message="The Lab is ready to be fixed. Enjoy!"
        )
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
    resource_statuses = []

    for expected in expected_docs:
        res_status = {
            "kind": expected.get("kind"),
            "name": expected.get("metadata", {}).get("name"),
            "namespace": expected.get("metadata", {}).get("namespace", namespace),
            "status": "",
            "error": None,
        }
        try:
            live = get_live_resource(
                expected["kind"],
                expected["apiVersion"],
                expected["metadata"]["name"],
                expected["metadata"].get("namespace", namespace)
            )
            if compare_resources(expected, live):
                if is_resource_ready(expected["kind"], live):
                    res_status["status"] = "Ready"
                else:
                    res_status["status"] = "NotReady"
                    res_status["error"] = f"{expected['kind']} is not ready"
                    all_match = False
            else:
                res_status["status"] = "NotMatching"
                mismatches = find_mismatches(expected, live)
                logging.debug(f"[validate_lab] Mismatches for {res_status['kind']} {res_status['name']}: {mismatches}")
                if not mismatches:
                    if is_resource_ready(expected["kind"], live):
                        res_status["status"] = "Ready"
                    else:
                        res_status["status"] = "NotReady"
                        res_status["error"] = f"{expected['kind']} is not ready"
                        all_match = False
                else:
                    res_status["status"] = "NotMatching"
                    res_status["mismatches"] = mismatches
                    all_match = False
        except ApiException as e:
            if e.status == 404:
                res_status["status"] = "NotFound"
                res_status["error"] = (
                    f"Resource {expected['kind']}/{expected['metadata']['name']} not found in namespace "
                    f"{expected['metadata'].get('namespace', namespace)}"
                )
                all_match = False
            else:
                res_status["status"] = "Error"
                res_status["error"] = f"Failed to get live resource: {e.reason}"
                all_match = False
        except Exception as e:
            res_status["status"] = "Error"
            res_status["error"] = f"Failed to get live resource: {str(e)}"
            all_match = False

        resource_statuses.append(res_status)

    # Before setting patch.status["resources"]:
    for res_status in resource_statuses:
        if "error" not in res_status:
            res_status["error"] = None
    resource_statuses.sort(key=lambda r: (r["kind"], r["namespace"], r["name"]))
    current = body.get("status", {}).get("resources")
    if current != resource_statuses:
        patch.status["resources"] = resource_statuses
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
            message="The Lab's current state does not match the expected state. Keep looking for the issue."
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
            elif kind == "Service":
                core_v1.delete_namespaced_service(res_name, res_ns)
            elif kind == "PersistentVolumeClaim":
                core_v1.delete_namespaced_persistent_volume_claim(res_name, res_ns)
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
