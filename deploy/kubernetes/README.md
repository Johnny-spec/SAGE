# Kubernetes pilot deployment

This manifest intentionally runs one SAGE API replica. The current control plane and Run/Plan/Memory/Invocation/HumanTask query stores use local SQLite and therefore are not multi-writer providers. `SAGE_REPLICA_COUNT` rejects unsafe production replica counts greater than one.

Before deployment:

1. Build and publish the image, then replace `ghcr.io/your-org/sage:0.1.0`.
2. Provide PostgreSQL for the official LangGraph checkpointer.
3. Provide Temporal; the manifest deploys the generic worker on `sage-workflows`.
4. Provide S3 or MinIO and create the configured bucket.
5. Create the `sage-secrets` Secret with the keys referenced by `sage.yaml`.
6. Configure an Entra Web API application and app roles used by Solution Releases.

Example Secret creation uses local values and is intentionally not checked in:

```powershell
kubectl create secret generic sage-secrets `
  --from-literal=entra-tenant-id='<tenant-guid>' `
  --from-literal=entra-audience='<api-client-id>' `
  --from-literal=checkpoint-database-url='<postgres-connection-string>' `
  --from-literal=temporal-target='<temporal-host:7233>' `
  --from-literal=worker-token-scope='api://<api-client-id>/.default' `
  --from-literal=s3-bucket='<bucket>'
```

Apply and verify:

```powershell
kubectl apply -f deploy/kubernetes/sage.yaml
kubectl rollout status deployment/sage
kubectl port-forward service/sage 8000:80
```

Do not scale beyond one replica until all control and query stores use PostgreSQL implementations and Generation artifacts use a shared immutable package provider.

The pilot manifest sets `SAGE_ADMIN_AUTH_ENABLED=false` for testability. Before exposing the service outside a controlled test network, set it to `true`; Entra app roles will then protect the management API.
