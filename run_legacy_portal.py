import os
from pathlib import Path
import sys

import uvicorn

# The server's embeddable runtime also serves the existing HTTP application.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from drive.legacy import legacy_upstream
from drive.legacy_portal import create_legacy_portal
from drive.legacy_preview import OfficePreview


if __name__ == '__main__':
    host = os.environ['LEGACY_PORTAL_HOST']
    upstream = legacy_upstream()
    if upstream is None:
        raise SystemExit('LEGACY_SEAFILE_URL is required')
    office = None
    if config := os.environ.get('LEGACY_PREVIEW_DOCS_CONFIG'):
        office = OfficePreview(config)
    elif secret := os.environ.get('OFFICE_JWT_SECRET'):
        office = OfficePreview(secret=secret)
    allowed_hosts = os.environ.get('LEGACY_PORTAL_ALLOWED_HOSTS', host).split(',')
    app = create_legacy_portal(upstream, [value.strip() for value in allowed_hosts], office,
                               unified_identity=os.environ.get('LEGACY_UNIFIED_IDENTITY')=='1')
    uvicorn.run(app, host=host, port=int(os.environ.get('LEGACY_PORTAL_PORT', '443')),
                ssl_certfile=os.environ['LEGACY_PORTAL_CERT'], ssl_keyfile=os.environ['LEGACY_PORTAL_KEY'],
                proxy_headers=os.environ.get('LEGACY_PORTAL_TRUST_PROXY') == '1',
                forwarded_allow_ips='127.0.0.1', access_log=False, server_header=False, limit_concurrency=256)
