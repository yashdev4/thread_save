import re

with open('src/thread_save/security/idempotency.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
    'return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()',
    'import hmac\n    key = b"threadvault_dev_key"\n    return hmac.new(key, text.strip().encode("utf-8"), hashlib.sha256).hexdigest()'
)

with open('src/thread_save/security/idempotency.py', 'w', encoding='utf-8') as f:
    f.write(text)
