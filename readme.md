# Kubernetes Lab Operator

This operator helps run **Kubernetes lab exercises** by applying given manifests and validating expected cluster state.

---

## How to run it locally using Minikube and Python

You can run the operator directly on your machine, connected to a Minikube cluster.

### Prerequisites

* Minikube installed and running ([https://minikube.sigs.k8s.io/docs/start/](https://minikube.sigs.k8s.io/docs/start/))
* `kubectl` configured to talk to your Minikube cluster
* Python 3.8+
* Operator dependencies:
  * `kopf==1.36.1`
  * `kubernetes==28.1.0`
  * `PyYAML==6.0.1`

### Steps

1. **Start Minikube**

    ```bash
    minikube start
    ```

2. **Make sure your local `kubectl` points to Minikube**

    ```bash
    kubectl config current-context  # should show minikube
    ```

3. **Install required Python dependencies**

    ```bash
    pip install kopf==1.36.1 kubernetes==28.1.0 PyYAML==6.0.1
    ```

4. **Deploy the CRD**

    ```bash
    kubectl apply -f crds/lab.yaml
    ```

5. **Run the operator Python script with `kopf`**

    ```bash
    kopf run labs-operator.py
    ```

    * This will start the operator locally.
    * It will watch for `Lab` resources on your Minikube cluster.
    * Any changes to the operator code will require restarting this command.

6. **Create and apply a Lab resource**

    You can now use either inline YAML or reference external YAML files for the `given` and `expected` fields.

    **Example using inline YAML:**
    ```yaml
    apiVersion: training.dev/v1
    kind: Lab
    metadata:
      name: example-lab
    spec:
      given: |
        apiVersion: v1
        kind: Pod
        metadata:
          name: busybox
          namespace: default
        spec:
          containers:
          - name: busybox-lab
            image: busybox
            command: ["sleep", "3600"]
      expected: |
        apiVersion: v1
        kind: Pod
        metadata:
          name: busybox
          namespace: default
        spec:
          containers:
          - name: busybox
            image: busybox
            command: ["sleep", "3600"]
    ```

    **Example using external YAML files:**
    ```yaml
    apiVersion: training.dev/v1
    kind: Lab
    metadata:
      name: example-lab-from-file
    spec:
      givenFile: example-labs/given.yaml
      expectedFile: example-labs/expected.yaml
    ```

    Save as `example-labs/example-lab.yaml` and apply:

    ```bash
    kubectl apply -f example-labs/example-lab.yaml
    ```

---

## Features

- Supports both inline YAML (`given`, `expected`) and referencing external YAML files (`givenFile`, `expectedFile`) for lab definitions.
- Applies resources in `spec.given` or from `spec.givenFile` to the cluster.
- Converts `spec.expected` or `spec.expectedFile` to a Secret and patches the Lab to use `expectedRef` (to prevent exposing the solution directly to the user and ensure consistent handling).
- Verifies the live cluster state matches the expected manifests (using a subset/deep comparison, ignoring extra fields set by Kubernetes).
- Updates `.status.ready`, `.status.message`, and `.status.error` accordingly.
- Emits Kubernetes events:
  * **While initializing:** "Please wait, the Lab is initializing and preparing resources."
  * **When fixed:** "The Lab is successfully fixed."
  * **When not fixed:** "The cluster state does not match the expected manifests. Keep looking for the issue and try again."
- When the Lab is deleted, all resources from `given`/`givenFile` and `expected`/`expectedFile` are deleted, including the operator-created Secret.

---

## How it works

```mermaid
sequenceDiagram
    participant User
    participant Kubernetes
    participant Operator

    User->>Kubernetes: Create Lab CR (kubectl apply)
    Kubernetes->>Operator: Triggers @on.create handler

    Operator->>Operator: Parse 'given', 'givenFile'
    Operator->>Operator: Create resources (apply_manifest)
    Operator->>Operator: Validate cluster state (validate_lab)
    Operator->>Kubernetes: Update Lab status (ready, message, error)
    Operator->>Kubernetes: Emit "Initializing" event (if needed)

    Note over Operator: Periodically or on Lab update

    Kubernetes->>Operator: Triggers @on.update or @timer handler
    Operator->>Operator: Parse 'expected', 'expectedFile', or 'expectedRef'
    Operator->>Operator: Validate cluster state (compare_resources)
    Operator->>Kubernetes: Update Lab status (ready, message, error)
    Operator->>Kubernetes: Emit "LabFixed" or "LabNotFixed" event (on status change)

    User->>Kubernetes: Delete Lab CR (kubectl delete)
    Kubernetes->>Operator: Triggers @on.delete handler
    Operator->>Operator: Parse manifests from 'given', 'givenFile', 'expected', 'expectedFile'
    Operator->>Kubernetes: Delete resources (Pod, ConfigMap, Secret, etc.)
    Operator->>Kubernetes: Wait for resources to be deleted
    Operator->>Kubernetes: Delete expected Secret (if exists)
```

---

## System Architecture

```mermaid
flowchart TD

  subgraph User
    U[User - kubectl]
  end

  subgraph "Control Plane"
    API[Kube API Server]
    ETCD[etcd - persistent store]
    CM[Controller Manager]
    SCH[Scheduler]
  end

  subgraph "Custom Operator Domain"
    CRD[Lab CRD - CustomResourceDefinition]
    CR[Lab CR - Custom Resource]
    OP[Lab Operator - controller]
    RES[Managed Resources Pod, PVC, Service, etc.]
  end

  %% User actions
  U -->|kubectl apply -f lab-crd.yaml| API
  U -->|kubectl apply -f lab.yaml| API
  U -->|kubectl get/describe lab| API

  %% CRD registration and CR creation
  API -->|stores schema| ETCD
  API -->|validates against CRD| CR
  API -->|stores CR| ETCD
  CR -->|conforms to| CRD

  %% Operator actions
  OP -->|watches for CR events via API| CR
  OP -->|uses REST API| API
  OP -->|creates/updates/deletes| RES
  OP -->|updates status| CR

  %% Control plane managing native resources
  API -->|notifies| CM
  CM -->|reconciles state| RES
  CM -->|stores updates| ETCD

  RES -->|scheduled by| SCH
  SCH -->|binds Pods to Nodes| RES

  %% Data storage
  API -->|persists all objects| ETCD

```

**Legend:**
- **User:** Uses `kubectl` to interact with the Kubernetes API server (e.g., apply Lab CRDs/CRs, get status).
- **Kube API Server:** Central API endpoint for all cluster operations; validates, stores, and serves resources.
- **etcd:** Persistent storage for all cluster state, including CRDs and CRs.
- **Controller Manager:** Reconciles desired and actual state for native resources.
- **Scheduler:** Assigns Pods to Nodes.
- **Lab CRD:** CustomResourceDefinition that defines the schema for Lab resources.
- **Lab CR:** A custom Lab resource instance representing a lab exercise.
- **Lab Operator:** Watches Lab CRs, manages lifecycle of related resources, updates status, and emits events.
- **Managed Resources:** Kubernetes objects (Pods, PVCs, Services, ConfigMaps, Secrets, etc.) created and managed by the operator.
- **RBAC/ServiceAccount:** Provides the operator with necessary permissions to watch and modify resources.

## Build and run with Docker

1. Build the Docker image:
    ```bash
    docker build -t kubernetes-labs-operator .
    ```
2. Run the container:
    ```bash
    docker run -p 8080:8080 -v ~/.kube:/root/.kube kubernetes-labs-operator
    ```
3. Open your browser and navigate to `http://localhost:8080`.

## Build and run with Podman

1. Build the image:
    ```bash
    podman build -t kubernetes-labs-operator .
    ```
2. Run the container:
    ```bash
    podman run -p 8080:8080 -v ~/.kube:/root/.kube:Z kubernetes-labs-operator
    ```
3. Open your browser and navigate to `http://localhost:8080`.

## How to deploy the operator on your cluster

You can deploy the operator as a Kubernetes Deployment using the provided `deployment.yaml` manifest.

### Prerequisites

* Your cluster is running and `kubectl` is configured to access it.
* The CRD for the Lab resource is installed:
    ```bash
    kubectl apply -f crds/lab.yaml
    ```

### Steps

1. **Deploy the operator and its RBAC**

    ```bash
    kubectl apply -f deployment.yaml
    ```

    This will create:
    - A Deployment running the operator container (using the image specified in the manifest)
    - A ServiceAccount for the operator
    - ClusterRole and ClusterRoleBinding for necessary permissions

2. **Verify the operator is running**

    ```bash
    kubectl get pods -l app=kubernetes-labs-operator
    kubectl logs deployment/kubernetes-labs-operator
    ```

3. **Create and apply a Lab resource**

    You can now create `Lab` resources as described above. The operator will watch for them and reconcile as expected.

---

**Note:**  
- The operator image is specified in `deployment.yaml` (e.g., `ghcr.io/skvortsovden/kubernetes-labs-operator:latest`).  
- If you push a new image, update the tag in `deployment.yaml` and re-apply it.

---

## Troubleshooting

* Inspect Lab status and events:

    ```bash
    kubectl describe lab example-lab
    ```

    Look for:
    - **Status:** `.status.ready`, `.status.message`, `.status.error`
    - **Events:** For initialization, success, or mismatch hints

### Notes

* The operator uses in-cluster config by default, but falls back to your local kubeconfig if run outside a cluster.
* You don’t need Docker or container rebuilds in this mode.
* Just create/update `Lab` CRs via `kubectl` as usual; the locally running operator will reconcile them.
* The `given`, `givenFile`, `expected`, and `expectedFile` blocks are supported for flexible lab definitions.

---