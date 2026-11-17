import json, sys
from harbor.decision_policy import validate
payload = json.load(sys.stdin)
print(json.dumps(validate(payload["policy"]["decisions"], payload["blocker"], payload["decision"])))
