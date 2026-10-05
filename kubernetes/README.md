# Kubernetes deployment (self-healing / replication demonstration)

## Run on kind / minikube
```bash
# 1. build and load images (from repo root)
for s in student payment academic; do docker build -f services/$s-service/Dockerfile -t uni-$s:1.0 .; done
docker build -f gateway/Dockerfile -t uni-gateway:1.0 .
kind load docker-image uni-student:1.0 uni-payment:1.0 uni-academic:1.0 uni-gateway:1.0     # or: minikube image load ...

# 2. deploy
kubectl apply -f kubernetes/00-namespace.yaml
kubectl -n uni create configmap db-init --from-file=database/schema_ft.sql --from-file=database/seed.sql
kubectl -n uni create secret generic uni-secrets --from-literal=POSTGRES_PASSWORD='s3cret' \
        --from-literal=DATABASE_URL='postgresql://uni:s3cret@postgres:5432/uni_ft'
kubectl apply -f kubernetes/01-config.yaml -f kubernetes/10-postgres.yaml -f kubernetes/20-services.yaml \
              -f kubernetes/30-gateway.yaml -f kubernetes/40-monitoring.yaml
# (02-secret.template.yaml shows the Secret layout; use the kubectl create secret command above instead of committing passwords)
kubectl -n uni get pods -o wide -w
kubectl -n uni port-forward svc/gateway 8000:8000
```

## Self-healing / replication experiments
| Experiment | Command | Expected |
|---|---|---|
| Application crash | `kubectl -n uni delete pod payment-0 --grace-period=0 --force` | requests keep succeeding via `payment-1`; the StatefulSet recreates `payment-0`; readiness probe re-admits it |
| Node failure | `kubectl drain <node> --ignore-daemonsets --force --delete-emptydir-data` (PDB keeps >=1 replica) | pods rescheduled; no total outage |
| Liveness restart | `kubectl -n uni exec payment-0 -- kill 1` | container restarted by kubelet (`RESTARTS` increments) |
| Gray failure | `curl -XPOST payment-0:8000/admin/fault -d '{"latency_ms":3000}'` (port-forward) | breaker/degradation as in the Compose demo |
| DB failure | `kubectl -n uni delete pod postgres-0` | services degrade/retry; recover when the pod is Ready (PVC keeps data) |

Design notes: StatefulSets are used for the application tiers only to obtain stable per-replica DNS names
(`student-0.student`, ...), because the gateway performs its own health-checked load balancing; every replica is spread
over nodes with `topologySpreadConstraints`; `PodDisruptionBudget`s protect against draining all replicas.
