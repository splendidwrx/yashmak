import asyncio
import socket
import ssl
import json
import os
import sys
import ipaddress
import traceback
import gzip
import random
import time

class core():
    def __init__(self):
        self.loop = asyncio.get_event_loop()
        if socket.has_dualstack_ipv6():
            listener = socket.create_server(address=('::', self.config['listen']), family=socket.AF_INET6,
                                            dualstack_ipv6=True)
        else:
            listener = socket.create_server(address=('0.0.0.0', self.config['listen']), family=socket.AF_INET,
                                            dualstack_ipv6=False)
        server = asyncio.start_server(client_connected_cb=self.handler, sock=listener, backlog=1024,ssl=self.get_context())
        self.init()
        self.loop.set_exception_handler(self.exception_handler)
        self.loop.create_task(server)
        self.loop.create_task(self.write_host())
        self.loop.run_forever()

    def init(self):
        s = socket.create_connection(('amazon.com', 443))
        ss = ssl.wrap_socket(s, server_side=False)
        ss.send(b'095fd1ca80a444b586d769cbf652478d')
        utc = time.mktime(time.strptime(str(ss.read(65535).split(b'\r\n')[2][6:])[2:-1],'%a, %d %b %Y %H:%M:%S GMT'))
        ss.close()
        self.utc_difference = utc - time.time()

    async def handler(self, client_reader, client_writer):
        try:
            server_writer = None
            tasks = None
            uuid = await asyncio.wait_for(client_reader.read(36),20)
            if uuid not in self.config['uuid']:
                for x in [b'0',b'1',b'2',b'3',b'4',b'5',b'6',b'7',b'8',b'9']:
                    if x in uuid:
                        await self.camouflage(client_reader,client_writer)
                        raise Exception
                await asyncio.sleep(60)
                raise Exception
            data = 0
            while data == 0:
                data = int.from_bytes((await asyncio.wait_for(client_reader.readexactly(2),20)), 'big',signed=True)
                if data > 0:
                    data = await asyncio.wait_for(client_reader.readexactly(data),20)
                    host, port = self.process(data)
                    await self.redirect(client_writer, host, uuid)
                    address = (await self.loop.getaddrinfo(host=host, port=port, family=0, type=socket.SOCK_STREAM))[0][4]
                    self.is_china_ip(address[0], host, uuid)
                    server_reader, server_writer = await asyncio.open_connection(host=address[0], port=address[1])
                    await asyncio.gather(self.switch(client_reader, server_writer, client_writer),
                                         self.switch(server_reader, client_writer, server_writer))
                elif data == -1:
                    await self.updater(client_writer, uuid, False)
                elif data == -2:
                    await self.TCP_ping(client_writer, client_reader)
                elif data == -3:
                    await self.updater(client_writer, uuid, True)
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(client_writer, server_writer)

    async def camouflage(self,reader,writer):
        GMT = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.localtime(self.utc_difference + time.time())).encode('utf-8')
        content = b'''<!DOCTYPE HTML PUBLIC "-//IETF//DTD HTML 2.0//EN">\r\n<html>\r\n<head><title>400 Bad Request</title></head>\r\n<body bgcolor="white">\r\n<h1>400 Bad Request</h1>\r\n<p>Your browser sent a request that this server could not understand. Sorry for the inconvenience.<br/>\r\nPlease report this message and include the following information to us.<br/>\r\nThank you very much!</p>\r\n<table>\r\n<tr>\r\n<td>URL:</td>\r\n<td>https://''' + self.config['domain'] + b'''</td>\r\n</tr>\r\n<tr>\r\n<td>Server:</td>\r\n<td>''' + self.config['servername'] + b'''</td>\r\n</tr>\r\n<tr>\r\n<td>Date:</td>\r\n<td>''' + time.strftime('%Y/%m/%d %H:%M:%S', time.localtime()).encode('utf-8') + b'''</td>\r\n</tr>\r\n</table>\r\n<hr/>Powered by Tengine<hr><center>tengine</center>\r\n</body>\r\n</html>\r\n'''
        writer.write(b'''HTTP/1.1 400 Bad Request\r\nServer: Tengine\r\nDate: ''' + GMT + b'''\r\nContent-Type: text/html\r\nContent-Length: ''' + str(len(content)).encode('utf-8') + b'''\r\nConnection: close\r\n\r\n''' + content)
        await writer.drain()
        await asyncio.wait_for(reader.readexactly(random.randint(500,2000)),20)

    async def switch(self, reader, writer, other):
        try:
            while True:
                data = await reader.read(16384)
                writer.write(data)
                await writer.drain()
                if data == b'':
                    break
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer, other)
        finally:
            await self.clean_up(writer, other)

    async def TCP_ping(self, writer, reader):
        try:
            time = await asyncio.wait_for(reader.read(8), 20)
            writer.write(time)
            await writer.drain()
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer)
        finally:
            await self.clean_up(writer)

    async def redirect(self, writer, host, uuid):
        try:
            URL = self.host_list[b'blacklist'][self.is_banned(host, uuid)]
            if URL != None:
                if URL[0:4] != b'http' and URL in self.host_list[b'blacklist']['tag']:
                    URL = self.host_list[b'blacklist']['tag'][URL]
                if URL[0:4] == b'http':
                    writer.write(b'''HTTP/1.1 301 Moved Permanently\r\nLocation: ''' + URL + b'''\r\nConnection: close\r\n\r\n''')
                else:
                    writer.write(b'''HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n''')
                await writer.drain()
                await self.clean_up(writer)
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer)

    async def updater(self, writer, uuid, compress=False):
        try:
            if len(uuid) != 36 or b'.' in uuid or b'/' in uuid or b'\\' in uuid:
                raise Exception
            if os.path.exists(self.local_path + '/Cache/' + uuid.decode('utf-8') + '.json'):
                with open(self.local_path + '/Cache/' + uuid.decode('utf-8') + '.json', 'rb') as file:
                    content = file.read()
                if compress:
                    content = gzip.compress(content, gzip._COMPRESS_LEVEL_FAST)
                writer.write(content)
                await writer.drain()
            else:
                writer.write(b'\n')
                await writer.drain()
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer, file)
        finally:
            await self.clean_up(writer, file)

    async def clean_up(self, writer1=None, writer2=None):
        try:
            writer1.close()
            await writer1.wait_closed()
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
        try:
            writer2.close()
            await writer2.wait_closed()
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None

    def exception_handler(self, loop, context):
        pass

    def process(self, data):
        return self.get_address(data)

    def get_address(self, data):
        position = data.find(b'\n')
        host = data[:position]
        position += 1
        port = data[position:data.find(b'\n', position)]
        return host, port

    def is_china_ip(self, ip, host, uuid):
        for x in [b'google',b'youtube',b'wikipedia',b'twitter']:
            if x in host:
                return False
        ip = ip.replace('::ffff:','',1)
        ip = int(ipaddress.ip_address(ip))
        left = 0
        right = len(self.geoip_list) - 1
        while left <= right:
            mid = left + (right - left) // 2
            if self.geoip_list[mid][0] <= ip and ip <= self.geoip_list[mid][1]:
                self.add_host(self.conclude(host), uuid)
                return True
            elif self.geoip_list[mid][1] < ip:
                left = mid + 1
            elif self.geoip_list[mid][0] > ip:
                right = mid - 1
        return False

    def is_banned(self, host, uuid):
        if host in self.host_list[b'blacklist']:
            return host
        sigment_length = len(host)
        while True:
            sigment_length = host.rfind(b'.', 0, sigment_length) - 1
            if sigment_length <= -1:
                break
            if host[sigment_length + 1:] in self.host_list[b'blacklist']:
                return host[sigment_length + 1:]
        return None

    def add_host(self, host, uuid):
        if uuid not in self.host_list:
            self.host_list[uuid] = set()
        self.host_list[uuid].add(host.replace(b'*', b''))

    async def write_host(self):
        def encode(host):
            if host[0] == 46:
                return '*' + host.decode('utf-8')
            return host.decode('utf-8')
        while True:
            for x in self.host_list:
                if x != b'blacklist':
                    with open(self.local_path + '/Cache/' + x.decode('utf-8') + '.json', 'w') as file:
                        json.dump(list(map(encode, list(self.host_list[x]))), file)
            await asyncio.sleep(60)

    def conclude(self, data):
        def detect(data):
            if data.count(b':') != 0 or data.count(b'.') <= 1:
                return False
            SLD = {b'com', b'net', b'org', b'gov',
                   b'co', b'edu', b'uk', b'us', b'kr',
                   b'au', b'hk', b'is', b'jpn', b'gb', b'gr'}
            if data.split(b'.')[-2] in SLD and data.count(b'.') < 3:
                return False
            for x in data:
                if x < 48 and x != 46 or x > 57:
                    return True
            return False

        if detect(data):
            return b'*' + data[data.find(b'.'):]
        else:
            return data

    def get_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols(['http/1.1'])
        context.load_cert_chain(self.config['cert'], self.config['key'])
        return context

class yashmak(core):
    def __init__(self):
        self.host_list = dict()
        self.geoip_list = []
        self.load_config()
        self.load_lists()

    def serve_forever(self):
        core.__init__(self)

    def load_config(self):
        self.local_path = os.path.abspath(os.path.dirname(sys.argv[0]))
        if os.path.exists(self.local_path + '/config.json'):
            with open(self.local_path + '/config.json', 'r') as file:
                content = file.read()
            content = self.translate(content)
            self.config = json.loads(content)
            self.config['uuid'] = self.UUID_detect(set(list(map(self.encode, self.config['uuid']))))
            self.config['listen'] = int(self.config['listen'])
            self.config['domain'] = self.config['domain'].encode('utf-8')
            self.config['servername'] = self.config['servername'].encode('utf-8')
        else:
            example = {'servername': '', 'domain': '', 'geoip': '','blacklist': '','cert': '', 'key': '', 'uuid': [''], 'listen': ''}
            with open(self.local_path + '/config.json', 'w') as file:
                json.dump(example, file, indent=4)

    def load_lists(self):
        with open(self.config['geoip'], 'r') as file:
            data = json.load(file)
        for x in data:
            network = ipaddress.ip_network(x)
            self.geoip_list.append([int(network[0]),int(network[-1])])
        self.geoip_list.sort()
        self.exception_list_name = self.config['uuid']
        for x in self.exception_list_name:
            self.host_list[x] = set()
            if os.path.exists(self.local_path + '/Cache/' + x.decode('utf-8') + '.json'):
                with open(self.local_path + '/Cache/' + x.decode('utf-8') + '.json', 'r') as file:
                    data = json.load(file)
                data = list(map(self.encode, data))
                for y in data:
                    self.host_list[x].add(y.replace(b'*', b''))
        with open(self.config['blacklist'], 'r') as file:
            data = json.load(file)
        for key in list(data):
            if key != 'tag':
                value = data[key].encode('utf-8')
                del data[key]
                data[key.replace('*', '').encode('utf-8')] = value
        for key in list(data['tag']):
            value = data['tag'][key].encode('utf-8')
            del data['tag'][key]
            data['tag'][key.replace('*', '').encode('utf-8')] = value
        data[None] = None
        self.host_list[b'blacklist'] = data

    def UUID_detect(self, UUIDs):
        for x in UUIDs:
            if len(x) != 36:
                raise Exception
        return UUIDs

    def translate(self, content):
        return content.replace('\\', '/')

    def encode(self, data):
        return data.encode('utf-8')


if __name__ == '__main__':
    server = yashmak()
    server.serve_forever()