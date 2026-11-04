"""One authorized connectivity probe. Never prints credentials or upstream bodies."""
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.request


def main() -> None:
    values = {}
    path = Path(__file__).resolve().parents[1] / '.env'
    if path.exists():
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    values.update({key: value for key, value in os.environ.items() if key.startswith('LLM_')})
    if not all(values.get(key) for key in ('LLM_BASE_URL', 'LLM_MODEL', 'LLM_API_KEY')):
        raise SystemExit('Set LLM_BASE_URL, LLM_MODEL and LLM_API_KEY in .env.')
    request = urllib.request.Request(values['LLM_BASE_URL'].rstrip('/') + '/chat/completions',
        data=json.dumps({'model': values['LLM_MODEL'], 'messages': [{'role': 'user', 'content': 'Reply with exactly: OK'}], 'max_tokens': 128, 'temperature': 0}).encode(),
        headers={'Authorization': 'Bearer ' + values['LLM_API_KEY'], 'Content-Type': 'application/json'})
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.load(response)
            text = payload['choices'][0]['message']['content']
            print(json.dumps({'status': response.status, 'model': payload.get('model'), 'response_ok': text.strip() == 'OK', 'seconds': round(time.monotonic() - started, 2), 'usage': payload.get('usage')}, ensure_ascii=False))
    except urllib.error.HTTPError as error:
        raise SystemExit(f'LLM connectivity check failed: HTTP {error.code}') from None
    except Exception:
        raise SystemExit('LLM connectivity check failed (network or response format).') from None


if __name__ == '__main__':
    main()
