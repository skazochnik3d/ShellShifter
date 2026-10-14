"""Connect the public HTTPS address after owner deployment; never takes tokens."""
import argparse
import json
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1]

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args): return None

def configure(origin):
    if not re.fullmatch(r'https://shellshifter-reports\.[a-z0-9-]+\.workers\.dev', origin):
        raise ValueError('Use the HTTPS workers.dev URL printed by Wrangler, without a trailing slash.')
    request = urllib.request.Request(origin+'/health', headers={'Accept':'application/json'})
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=15) as response:
        data = response.read(4097)
        if len(data)>4096 or response.status != 200:
            raise ValueError('Unexpected service response.')
        status = json.loads(data)
    if status != {'schema':1,'enabled':True}:
        raise ValueError('The service is not enabled. Check its secrets and bindings first.')
    endpoint = origin+'/v1/reports'
    destination = ROOT/'ShellShifter/uv_straight_report_config.py'
    destination.write_text('"""Public service address only. No keys or credentials belong in this file."""\n'
                           +'ENDPOINT = '+repr(endpoint)+'\n',encoding='utf8')
    print('Connected:',endpoint)
    print('Build new install archives; restart Maya after updating the plugin.')

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('origin',help='Public workers.dev origin; no token or password')
    configure(parser.parse_args().origin)
