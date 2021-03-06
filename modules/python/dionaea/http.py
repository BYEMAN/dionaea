#*************************************************************************
#*                               Dionaea
#*                           - catches bugs -
#*
#*
#*
#* Copyright (C) 2009  Paul Baecher & Markus Koetter & Mark Schloesser
#*
#* This program is free software; you can redistribute it and/or
#* modify it under the terms of the GNU General Public License
#* as published by the Free Software Foundation; either version 2
#* of the License, or (at your option) any later version.
#*
#* This program is distributed in the hope that it will be useful,
#* but WITHOUT ANY WARRANTY; without even the implied warranty of
#* MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#* GNU General Public License for more details.
#*
#* You should have received a copy of the GNU General Public License
#* along with this program; if not, write to the Free Software
#* Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#*
#*
#*             contact nepenthesdev@gmail.com
#*
#*******************************************************************************/


from dionaea import ServiceLoader
from dionaea.core import connection, g_dionaea, incident, ihandler
from dionaea.exception import ServiceConfigError
#from dionaea.services import g_slave
import struct
from collections import OrderedDict
import logging
import os
import sys
import datetime
import io
import cgi
import urllib.parse
import re
import tempfile

logger = logging.getLogger('http')
logger.setLevel(logging.DEBUG)


class HTTPService(ServiceLoader):
    name = "http"

    @classmethod
    def start(cls, addr, iface=None, config=None):
        if config is None:
            config = {}

        root_path = config.get("root")
        if not root_path:
            logger.warning("Root path not set skipping service")

        daemons = []

        for port in config.get("ports", []):
            daemon = httpd(proto="tcp")
            try:
                daemon.apply_config(config)
            except ServiceConfigError as e:
                logger.error(e.msg, *e.args)
                continue
            daemon.bind(addr, port, iface=iface)
            daemon.listen()
            daemons.append(daemon)

        for port in config.get("ssl_ports", []):
            daemon = httpd(proto="tls")
            try:
                daemon.apply_config(config)
            except ServiceConfigError as e:
                logger.error(e.msg, *e.args)
                continue
            daemon.bind(addr, port, iface=iface)
            daemon.listen()
            daemons.append(daemon)

        return daemons


class httpreq:
    def __init__(self, header):
        hlines = header.split(b'\n')
        req = hlines[0]
        reqparts = req.split(b" ")
        self.type = reqparts[0]
        self.path = urllib.parse.unquote(reqparts[1].decode('utf-8'))
        self.version = reqparts[2]
        r = self.version.find(b"\r")
        if r:
            self.version = self.version[:r]
        self.headers = {}
        for hline in hlines[1:]:
            if hline[len(hline)-1] == 13: # \r
                hline = hline[:len(hline)-1]
            hset = hline.split(b":", 1)
            self.headers[hset[0].lower()] = hset[1].strip()

    def log_req(self):
        logger.debug(
            self.type + b" " + self.path.encode('utf-8') + b" " + self.version)
        for i in self.headers:
            logger.debug(i + b":" + self.headers[i])


class Headers(object):
    def __init__(self, headers, global_headers=None, filename_pattern=None, methods=None, status_codes=None):
        if global_headers is not None:
            headers = global_headers + headers

        self.headers = OrderedDict(headers)
        logger.debug("Headers: %r", self.headers)

        self.methods = None
        if methods:
            self.methods = methods[:]

        self.filename_pattern = None
        if filename_pattern:
            self.filename_pattern = re.compile(filename_pattern)

        self.status_codes = None
        if status_codes:
            self.status_codes = status_codes[:]

    def match(self, code, method=None, filename=None):
        if self.methods:
            if not method or method not in self.methods:
                return False

        if self.filename_pattern:
            if not filename or not self.filename_pattern.match(filename):
                return False

        if self.status_codes:
            if not code or code not in self.status_codes:
                return False

        return True

    def prepare(self, values):
        for n, v in self.headers.items():
            try:
                yield (n, v.format(**values))
            except KeyError as e:
                logger.warning("Key error in header: %s: %s" % (n, v), exc_info=True)

    def send(self, connection, values):
        for header in self.prepare(values):
            connection.send_header(header[0], header[1])


class httpd(connection):
    shared_config_values = ["default_headers", "download_dir", "download_suffix", "headers", "root", "rwchunksize", "root"]

    def __init__(self, proto="tcp"):
        logger.debug("http test")
        connection.__init__(self, proto)
        self.state = 'HEADER'
        self.rwchunksize = 64*1024
        self._out.speed.limit = 16*1024
        self.env = None
        self.boundary = None
        self.fp_tmp = None
        self.cur_length = 0

        self.headers = []
        self.max_request_size = 32768 * 1024
        self.download_dir = None
        self.download_suffix = ".tmp"
        self._default_headers = [
            ("Content-Type", "{content_type}"),
            ("Content-Length", "{content_length}"),
            ("Connection", "{connection}")
        ]
        self.default_headers = Headers(self._default_headers)
        self.root = None

    def _get_headers(self, code=None, filename=None, method=None):
        for header in self.headers:
            if header.match(code=code, filename=filename, method=method):
                return header
        return self.default_headers

    def apply_config(self, config):
        dionaea_config = g_dionaea.config().get("dionaea")
        self.download_dir = dionaea_config.get("download.dir")
        self.download_suffix = dionaea_config.get("download.suffix", ".tmp")

        default_headers = config.get("default_headers", self._default_headers)
        global_headers = config.get('global_headers', [])

        self.default_headers = Headers(default_headers, global_headers=global_headers)

        headers = config.get('headers', [])
        for header in headers:
            self.headers.append(
                Headers(
                    header.get("headers", []),
                    global_headers=global_headers,
                    filename_pattern=header.get("filename_pattern"),
                    status_codes=header.get("status_codes")
                )
            )

        self.headers.append(
            Headers(
                [
                    ("Location", "{location}"),
                    ("Connection", "{connection}")
                ],
                global_headers=global_headers,
                status_codes=[301, 302]
            )
        )

        self.headers.append(
            Headers(
                [
                    ("Allow", "{allow}"),
                    ("Connection", "{connection}")
                ],
                global_headers=global_headers,
                methods=["options"]
            )
        )

        conf_max_request_size = config.get("max_request_size")
        if conf_max_request_size is not None:
            try:
                self.max_request_size = int(conf_max_request_size) * 1024
            except ValueError:
                logger.warning("Error while converting 'max_request_size' to an integer value. Using default value.")

        self.root = config.get("root")
        if self.root is None:
            logger.warningfigError("Root directory not configured")
        else:
            if not os.path.isdir(self.root):
                logger.warning("Root path '%s' is not a directory", self.root)
            elif not os.access(self.root, os.R_OK):
                logger.warning("Unable to read content of root directory '%s'", self.root)

    def handle_origin(self, parent):
        pass

    def handle_established(self):
        self.timeouts.idle = 10
        self.processors()

    def chroot(self, path):
        self.root = path

    def handle_io_in(self, data):
        if self.state == 'HEADER':
            # End Of Head
            eoh = data.find(b'\r\n\r\n')
            # Start Of Content
            soc = eoh + 4

            if eoh == -1:
                eoh = data.find(b'\n\n')
                soc = eoh + 2
            if eoh == -1:
                return 0

            header = data[0:eoh]
            data = data[soc:]
            self.header = httpreq(header)
            self.header.log_req()

            if self.header.type == b'GET':
                self.handle_GET()
                return len(data)

            elif self.header.type == b'HEAD':
                self.handle_HEAD()
                return len(data)

            elif self.header.type == b'POST':
                if b'content-type' not in self.header.headers and b'content-type' not in self.header.headers:
                    self.handle_POST()
                    return len(data)

                try:
                    # at least this information are needed for
                    # cgi.FieldStorage() to parse the content
                    self.env = {
                        'REQUEST_METHOD':'POST',
                        'CONTENT_LENGTH': self.header.headers[b'content-length'].decode("utf-8"),
                        'CONTENT_TYPE': self.header.headers[b'content-type'].decode("utf-8")
                    }
                except:
                    # ignore decode() errors
                    self.handle_POST()
                    return len(data)

                m = re.compile("multipart/form-data;\s*boundary=(?P<boundary>.*)",
                               re.IGNORECASE).match(self.env['CONTENT_TYPE'])

                if not m:
                    self.handle_POST()
                    return len(data)


                self.state = 'POST'
                # More on boundaries see:
                # http://www.apps.ietf.org/rfc/rfc2046.html#sec-5.1.1
                self.boundary = bytes(
                    "--" + m.group("boundary") + "--\r\n", 'utf-8')

                # dump post content to file
                self.fp_tmp = tempfile.NamedTemporaryFile(
                    delete=False,
                    dir=self.download_dir,
                    prefix='http-',
                    suffix=self.download_suffix
                )

                pos = data.find(self.boundary)
                # ending boundary not found
                if pos < 0:
                    self.cur_length = soc
                    return soc

                self.fp_tmp.write(data[:pos])
                self.handle_POST()
                return soc + pos

            elif self.header.type == b'OPTIONS':
                self.handle_OPTIONS()
                return len(data)

            # ToDo
            #elif self.header.type == b'PUT':
            #   self.handle_PUT()

            # method not found
            self.handle_unknown()
            return len(data)

        elif self.state == 'POST':
            pos = data.find(self.boundary)
            length = len(data)
            if pos < 0:
                # boundary not found
                l = length - len(self.boundary)
                if l < 0:
                    l = 0
                self.cur_length = self.cur_length + l

                if self.cur_length > self.max_request_size:
                    # Close connection if request is to large.
                    # RFC2616: "The server MAY close the connection to prevent the client from continuing the request."
                    # http://www.w3.org/Protocols/rfc2616/rfc2616-sec10.html#sec10.4.14
                    x = self.send_error(413)
                    if x:
                        self.copyfile(x)
                    return length
                self.fp_tmp.write(data[:l])
                return l

            # boundary found
            self.fp_tmp.write(data[:pos+len(self.boundary)])
            self.handle_POST()
            return pos + len(self.boundary)

        elif self.state == 'PUT':
            logger.debug("putting to me")
        elif self.state == 'SENDFILE':
            logger.debug("sending file")
            return 0

        return len(data)


    def handle_GET(self):
        """Handle the GET method. Send the header and the file."""
        x = self.send_head()
        if x :
            self.copyfile(x)

    def handle_HEAD(self):
        """Handle the HEAD method. Send only the header but not the file."""
        x = self.send_head()
        if x :
            x.close()
            self.close()

    def handle_OPTIONS(self):
        """
        Handle the OPTIONS method. Returns the HTTP methods that the server supports.
        """
        self.send_response(200)
        headers = self._get_headers(code=200, method="options")
        headers.send(
            self,
            {
                "allow": "OPTIONS, GET, HEAD, POST",
                "connection": "close",
                "content_length": 0
            }
        )
        self.end_headers()
        self.close()

    def handle_POST(self):
        """
        Handle the POST method. Send the head and the file. But ignore the POST params.
        Use the bistreams for a better analysis.
        """
        if self.fp_tmp != None:
            self.fp_tmp.seek(0)
            form = cgi.FieldStorage(fp = self.fp_tmp, environ = self.env)
            for field_name in form.keys():
                # dump only files
                if form[field_name].filename == None:
                    continue

                fp_post = form[field_name].file

                data = fp_post.read(4096)

                # don't handle empty files
                if len(data) == 0:
                    continue

                fp_tmp = tempfile.NamedTemporaryFile(
                    delete=False,
                    dir=self.download_dir,
                    prefix='http-',
                    suffix=self.download_suffix
                )
                while data != b'':
                    fp_tmp.write(data)
                    data = fp_post.read(4096)

                icd = incident("dionaea.download.complete")
                icd.path = fp_tmp.name
                icd.con = self
                # We need the url for logging
                icd.url = ""
                fp_tmp.close()
                icd.report()
                os.unlink(fp_tmp.name)

            os.unlink(self.fp_tmp.name)

        x = self.send_head()
        if x :
            self.copyfile(x)

    def handle_PUT(self):
        pass

    def handle_unknown(self):
        x = self.send_error(501)
        if x:
            self.copyfile(x)

    def copyfile(self, f):
        self.file = f
        self.state = 'SENDFILE'
        self.handle_io_out()

    def send_head(self):
        rpath = os.path.normpath(self.header.path)
        fpath = os.path.join(self.root, rpath[1:])
        apath = os.path.abspath(fpath)
        aroot = os.path.abspath(self.root)
        logger.debug("root %s aroot %s rpath %s fpath %s apath %s" %
                     (self.root, aroot, rpath, fpath, apath))
        if not apath.startswith(aroot):
            self.send_response(404, "File not found")
            self.end_headers()
            self.close()
        if os.path.exists(apath):
            if os.path.isdir(apath):
                if self.header.path.endswith('/'):
                    testpath = os.path.join(apath, "index.html")
                    if os.path.isfile(testpath):
                        apath = testpath
            if os.path.isdir(apath):
                if not self.header.path.endswith('/'):
                    self.send_response(301)
                    headers = self._get_headers(code=301)
                    headers.send(
                        self,
                        {
                            "connection": "close",
                            "location": self.header.path + "/"
                        }
                    )
                    self.end_headers()
                    self.close()
                    return None
                return self.list_directory(apath)
            elif os.path.isfile(apath):
                f = io.open(apath, 'rb')
                self.send_response(200)
                headers = self._get_headers(code=200, filename=apath)
                headers.send(
                    self,
                    {
                        "connection": "close",
                        "content_length": os.stat(apath).st_size
                    }
                )
                self.end_headers()
                return f
            else:
                return self.send_error(404)
        else:
            return self.send_error(404)
        return None

    def handle_io_out(self):
        logger.debug("handle_io_out")
        if self.state == 'SENDFILE':
            w = self.file.read(self.rwchunksize)
            if len(w) > 0:
                self.send(w)
            # send call call handle_io_out
            # to avoid double close warning we check state
            if len(w) < self.rwchunksize and self.state != None:
                self.state = None
                self.close()
                self.file.close()

    def list_directory(self, path):
        """Helper to produce a directory listing (absent index.html).

        Return value is either a file object, or None (indicating an
        error).  In either case, the headers are sent, making the
        interface the same as for send_head().

        """
        try:
            list = os.listdir(path)
            list.append("..")
        except os.error:
            self.send_error(404, "No permission to list directory")
            return None
        list.sort(key=lambda a: a.lower())
        r = []
        displaypath = cgi.escape(self.header.path)
        r.append('<!DOCTYPE html PUBLIC "-//W3C//DTD HTML 3.2 Final//EN">')
        r.append("<html>\n<title>Directory listing for %s</title>\n" %
                 displaypath)
        r.append("<body>\n<h2>Directory listing for %s</h2>\n" % displaypath)
        r.append("<hr>\n<ul>\n")
        for name in list:
            fullname = os.path.join(path, name)
            displayname = linkname = name
            # Append / for directories or @ for symbolic links
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"
                # Note: a link to a directory displays with @ and links with /
            r.append('<li><a href="%s">%s</a>\n' %
                     (urllib.parse.quote(linkname), cgi.escape(displayname)))


        r.append("</ul>\n<hr>\n</body>\n</html>\n")
        enc = sys.getfilesystemencoding()
        encoded = ''.join(r).encode(enc)
        self.send_response(200)
        headers = self._get_headers(code=200)
        headers.send(
            self,
            {
                "connection": "close",
                "content_length": len(encoded),
                "content_type": "text/html; charset=%s" % enc
            }
        )
        self.end_headers()
        f = io.BytesIO()
        f.write(encoded)
        f.seek(0)
        return f

    def send_response(self, code, message=None):
        if message is None:
            if code in self.responses:
                message = self.responses[code][0]
            else:
                message = ''
        self.send("%s %d %s\r\n" % ("HTTP/1.1", code, message))

    def send_error(self, code, message = None):
        if message is None:
            if code in self.responses:
                message = self.responses[code][0]
            else:
                message = ''
        enc = sys.getfilesystemencoding()

        r = []
        r.append('<?xml version="1.0" encoding="%s"?>\n' % (enc))
        r.append(
            '<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN"\n')
        r.append(
            '         "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">\n')
        r.append(
            '<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="en" lang="en">\n')
        r.append(' <head>\n')
        r.append('  <title>%d - %s</title>\n' % (code, message))
        r.append(' </head>\n')
        r.append(' <body>\n')
        r.append('  <h1>%d - %s</h1>\n' % (code, message))
        r.append(' </body>\n')
        r.append('</html>\n')

        encoded = ''.join(r).encode(enc)

        self.send_response(code, message)

        headers = self._get_headers(code=code)
        headers.send(
            self,
            {
                "connection": "close",
                "content_length": len(encoded),
                "content_type": "text/html; charset=%s" % enc
            }
        )
        self.end_headers()

        f = io.BytesIO()
        f.write(encoded)
        f.seek(0)
        return f

    def send_header(self, key, value):
        self.send("%s: %s\r\n" % (key, value))

    def end_headers(self):
        self.send("\r\n")

    def handle_disconnect(self):
        return False

    def handle_timeout_idle(self):
        return False

    responses = {
        100: ('Continue', 'Request received, please continue'),
        101: ('Switching Protocols',
              'Switching to new protocol; obey Upgrade header'),

        200: ('OK', 'Request fulfilled, document follows'),
        201: ('Created', 'Document created, URL follows'),
        202: ('Accepted',
              'Request accepted, processing continues off-line'),
        203: ('Non-Authoritative Information', 'Request fulfilled from cache'),
        204: ('No Content', 'Request fulfilled, nothing follows'),
        205: ('Reset Content', 'Clear input form for further input.'),
        206: ('Partial Content', 'Partial content follows.'),

        300: ('Multiple Choices',
              'Object has several resources -- see URI list'),
        301: ('Moved Permanently', 'Object moved permanently -- see URI list'),
        302: ('Found', 'Object moved temporarily -- see URI list'),
        303: ('See Other', 'Object moved -- see Method and URL list'),
        304: ('Not Modified',
              'Document has not changed since given time'),
        305: ('Use Proxy',
              'You must use proxy specified in Location to access this '
              'resource.'),
        307: ('Temporary Redirect',
              'Object moved temporarily -- see URI list'),

        400: ('Bad Request',
              'Bad request syntax or unsupported method'),
        401: ('Unauthorized',
              'No permission -- see authorization schemes'),
        402: ('Payment Required',
              'No payment -- see charging schemes'),
        403: ('Forbidden',
              'Request forbidden -- authorization will not help'),
        404: ('Not Found', 'Nothing matches the given URI'),
        405: ('Method Not Allowed',
              'Specified method is invalid for this server.'),
        406: ('Not Acceptable', 'URI not available in preferred format.'),
        407: ('Proxy Authentication Required', 'You must authenticate with '
              'this proxy before proceeding.'),
        408: ('Request Timeout', 'Request timed out; try again later.'),
        409: ('Conflict', 'Request conflict.'),
        410: ('Gone',
              'URI no longer exists and has been permanently removed.'),
        411: ('Length Required', 'Client must specify Content-Length.'),
        412: ('Precondition Failed', 'Precondition in headers is false.'),
        413: ('Request Entity Too Large', 'Entity is too large.'),
        414: ('Request-URI Too Long', 'URI is too long.'),
        415: ('Unsupported Media Type', 'Entity body in unsupported format.'),
        416: ('Requested Range Not Satisfiable',
              'Cannot satisfy request range.'),
        417: ('Expectation Failed',
              'Expect condition could not be satisfied.'),

        500: ('Internal Server Error', 'Server got itself in trouble'),
        501: ('Not Implemented',
              'Server does not support this operation'),
        502: ('Bad Gateway', 'Invalid responses from another server/proxy.'),
        503: ('Service Unavailable',
              'The server cannot process the request due to a high load'),
        504: ('Gateway Timeout',
              'The gateway server did not receive a timely response'),
        505: ('HTTP Version Not Supported', 'Cannot fulfill request.'),
    }
