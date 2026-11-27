"""Read Seafile documents and produce read-only previews."""
import base64
import hashlib
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from io import BytesIO
from pathlib import Path, PurePosixPath
import secrets
from threading import BoundedSemaphore, Thread
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import zipfile

from .store import DriveError
from .office import origin, service_base, sign


OFFICE = {'doc','docx','xls','xlsx','ppt','pptx','odt','ods','odp','rtf'}
TEXT = {'txt','md','csv','tsv','log','json','xml','ini','yaml','yml'}
MEDIA = {'pdf':'application/pdf','png':'image/png','jpg':'image/jpeg','jpeg':'image/jpeg',
         'gif':'image/gif','webp':'image/webp','bmp':'image/bmp'}
LIMIT = 50 * 1024**2


def read_limited(response, maximum):
    length = response.headers.get('Content-Length')
    if length and length.isdecimal() and int(length) > maximum:
        raise DriveError(413, '文件较大，请下载后查看')
    result = response.read(maximum + 1)
    if len(result) > maximum:
        raise DriveError(413, '文件较大，请下载后查看')
    return result


def preview_bytes(upstream, token, repo, path, office):
    extension = PurePosixPath(path).suffix.lower().lstrip('.')
    if extension not in MEDIA and extension not in TEXT and extension not in OFFICE:
        raise DriveError(415, '此格式暂不支持在线预览，请下载后查看')
    if extension in OFFICE and office is None:
        raise DriveError(503, 'Office 预览服务暂不可用，请下载后查看')
    connection, response = upstream.download(token, repo, path)
    try:
        data = read_limited(response, 2 * 1024**2 if extension in TEXT else LIMIT)
    except OSError:
        raise DriveError(502, '读取文件失败，请重试') from None
    finally:
        connection.close()
    if extension in OFFICE:
        if extension in ('docx','xlsx','pptx','odt','ods','odp'):
            try:
                with zipfile.ZipFile(BytesIO(data)) as archive:
                    expected = {'docx':'word/document.xml','xlsx':'xl/workbook.xml','pptx':'ppt/presentation.xml',
                                'odt':'content.xml','ods':'content.xml','odp':'content.xml'}[extension]
                    if expected not in archive.namelist():
                        raise zipfile.BadZipFile()
            except zipfile.BadZipFile:
                raise DriveError(422, '此文件可能已加密或不是标准 Office 格式，请下载后查看') from None
        return office.convert(data, extension), 'application/pdf'
    if extension in TEXT:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeError:
            text = data.decode('utf-16' if data.startswith((b'\xff\xfe',b'\xfe\xff')) else 'gb18030', errors='replace')
        return text.encode(), 'text/plain; charset=utf-8'
    if extension == 'pdf' and not data.startswith(b'%PDF-'):
        raise DriveError(422, '此文件无法识别为 PDF，请下载后查看')
    return data, MEDIA[extension]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_):
        return None


class OfficePreview:
    def __init__(self, config_path=None, *, secret=None):
        if secret is None:
            settings = json.loads(Path(config_path).read_text(encoding='utf-8-sig'))['services']['CoAuthoring']
            secret = settings['secret']['inbox']['string']
        self.secret = secret.encode()
        if len(self.secret) < 32:
            raise ValueError('Invalid document conversion secret')
        self.base_url = service_base('LEGACY_PREVIEW_URL', 'http://127.0.0.1:8080')
        self.loopback_converter = os.environ.get('LEGACY_PREVIEW_LOOPBACK_CONVERTER') == '1'
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.slots = BoundedSemaphore(2)

    def convert(self, source, extension):
        if not self.slots.acquire(blocking=False):
            raise DriveError(503, '正在预览的文件较多，请稍后重试')
        try:
            if self.loopback_converter:
                return self._convert_loopback(source, extension)
            return self._convert(source, extension)
        except (OSError, URLError, ValueError):
            raise DriveError(502, '此文件暂时无法预览，请下载后查看') from None
        finally:
            self.slots.release()

    def _convert_loopback(self, source, extension):
        # Older Docs versions fetch a URL. The source is available briefly on
        # loopback to a document service sharing this container's network.
        path = '/' + secrets.token_urlsafe(32) + '.' + extension

        class Source(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path != path:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Content-Length', str(len(source)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(source)

        with HTTPServer(('127.0.0.1', 0), Source) as server:
            worker = Thread(target=server.serve_forever, kwargs={'poll_interval': 0.1}, daemon=True)
            worker.start()
            try:
                params = {'async': False, 'filetype': extension, 'outputtype': 'pdf',
                          'key': secrets.token_hex(16), 'title': 'preview.' + extension,
                          'url': f'http://127.0.0.1:{server.server_port}{path}'}
                token = sign(params, self.secret)
                request = Request(self.base_url + '/converter', json.dumps({**params, 'token': token}).encode(),
                                  {'Content-Type': 'application/json', 'Accept': 'application/json',
                                   'Authorization': 'Bearer ' + token})
                with self.opener.open(request, timeout=45) as response:
                    result = json.loads(read_limited(response, 64 * 1024))
                if not isinstance(result, dict) or not result.get('endConvert'):
                    raise DriveError(502, '此 Office 文件暂时无法生成预览，请下载后查看')
                target = result.get('fileUrl', '')
                parsed = urlsplit(target)
                if (origin(target) != self.base_url or parsed.fragment
                        or not parsed.path.startswith('/cache/files/')):
                    raise ValueError('Unexpected document conversion result origin')
                with self.opener.open(target, timeout=15) as response:
                    pdf = read_limited(response, LIMIT)
                if not pdf.startswith(b'%PDF-'):
                    raise ValueError('Invalid document conversion result')
                return pdf
            finally:
                server.shutdown()
                worker.join(timeout=2)

    def _convert(self, source, extension, retry=True):
        def b64(value):
            return base64.urlsafe_b64encode(value).rstrip(b'=')

        params = {'filetype': extension, 'outputtype': 'pdf', 'operation': 'converter',
                  'title': 'preview.' + extension, 'key': secrets.token_hex(16)}
        body = b64(b'{"alg":"HS256","typ":"JWT"}') + b'.' + b64(json.dumps(params).encode())
        token = (body + b'.' + b64(hmac.digest(self.secret, body, hashlib.sha256))).decode()
        boundary = 'Suyan' + secrets.token_hex(20)
        multipart = (f'--{boundary}\r\nContent-Disposition: form-data; name="token"\r\n\r\n{token}\r\n'
                     f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="preview.{extension}"\r\n'
                     'Content-Type: application/octet-stream\r\n\r\n').encode() + source + f'\r\n--{boundary}--\r\n'.encode()
        req = Request(self.base_url + '/converter/from-file', multipart,
                      {'Content-Type': 'multipart/form-data; boundary=' + boundary})
        with self.opener.open(req, timeout=45) as response:
            pdf = read_limited(response, LIMIT)
        if not pdf.startswith(b'%PDF-'):
            try:
                error = json.loads(pdf).get('error')
            except (ValueError, AttributeError):
                error = None
            if error == -3 and retry:
                return self._convert(source, extension, retry=False)
            raise DriveError(502, '此 Office 文件暂时无法生成预览，请下载后查看')
        return pdf
