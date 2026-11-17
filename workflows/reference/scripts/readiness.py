import json, sys
from harbor.readiness import evaluate
payload = json.load(sys.stdin)
print(json.dumps(evaluate(payload['policy'], payload)))
