#!/usr/bin/env python3.8
import asyncio
import socket
import ssl
import json
import os
import sys
import ipaddress
import traceback
import gzip
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
        server = asyncio.start_server(client_connected_cb=self.handler, sock=listener, backlog=1024)
        self.context = self.get_context()
        self.connection_pool = []
        self.loop.set_exception_handler(self.exception_handler)
        self.loop.create_task(server)
        self.loop.create_task(self.pool())
        self.loop.create_task(self.pool_health())
        self.loop.create_task(self.update_white_list())
        self.loop.run_forever()

    async def handler(self, client_reader, client_writer):
        try:
            server_writer = None
            tasks = None
            data = await asyncio.wait_for(client_reader.read(65535),20)
            if data == b'':
                raise Exception
            data, URL, host, port, request_type = await self.process(data, client_reader, client_writer)
            await self.redirect(client_writer,host,URL)
            server_reader, server_writer = await self.proxy(host,port,request_type,data,client_reader,client_writer,self.get_type(host))
            await asyncio.gather(self.switch(client_reader, server_writer, client_writer, True),
                                 self.switch(server_reader, client_writer, server_writer, False))
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(client_writer, server_writer)

    async def switch(self, reader, writer, other, up):
        try:
            if not up:
                while True:
                    data = await reader.read(16384)
                    writer.write(data)
                    await writer.drain()
                    if data == b'':
                        break
            else:
                while True:
                    data = await reader.read(16384)
                    if data[:3] == b'GET' or data[:4] == b'POST':
                        URL, host, port = self.get_http_address(data, 1)
                        data = self.get_response(data, host, port)
                    writer.write(data)
                    await writer.drain()
                    if data == b'':
                        break
            await self.clean_up(writer, other)
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer, other)

    async def redirect(self, writer, host, URL):
        try:
            def HSTS(HSTS_list,host):
                if host in HSTS_list:
                    return True
                sigment_length = len(host)
                while True:
                    sigment_length = host.rfind(b'.', 0, sigment_length) - 1
                    if sigment_length <= -1:
                        break
                    if host[sigment_length + 1:] in HSTS_list:
                        return True

            if URL != None and HSTS(self.HSTS_list,host):
                writer.write(b'''HTTP/1.1 301 Moved Permanently\r\nLocation: ''' + URL + b'''\r\nConnection: close\r\n\r\n''')
                await writer.drain()
                await self.clean_up(writer)
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(writer)

    async def proxy(self, host, port, request_type, data, client_reader, client_writer, type):
        server_writer = None
        try:
            if type:
                if self.connection_pool == []:
                    server_reader, server_writer = await asyncio.open_connection(host=self.config['host'],
                                                                                 port=self.config['port'],
                                                                                 ssl=self.context,
                                                                                 server_hostname=self.config['host'])
                    server_writer.write(self.config['uuid'])
                    await server_writer.drain()
                else:
                    server_reader, server_writer = self.connection_pool.pop(0)
                server_writer.write(int.to_bytes(len(host + b'\n' + port + b'\n'), 2, 'big', signed=True))
                await server_writer.drain()
                server_writer.write(host + b'\n' + port + b'\n')
                await server_writer.drain()
            else:
                address = (await self.loop.getaddrinfo(host=host, port=port, family=0, type=socket.SOCK_STREAM))[0][4]
                if address[0] != '127.0.0.1':
                    server_reader, server_writer = await asyncio.open_connection(host=address[0], port=address[1])
                else:
                    if not request_type:
                        client_writer.write(b'''HTTP/1.1 404 Not Found\r\nProxy-Connection: close\r\n\r\n''')
                        await client_writer.drain()
                    raise Exception
            if not request_type:
                client_writer.write(b'''HTTP/1.1 200 Connection Established\r\nProxy-Connection: close\r\n\r\n''')
                await client_writer.drain()
            elif data != None:
                server_writer.write(data)
                await server_writer.drain()
            return server_reader, server_writer
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            await self.clean_up(client_writer, server_writer)

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

    async def pool(self):
        pool_max_size = 8
        self.unhealthy = 0
        while True:
            while len(self.connection_pool) < pool_max_size:
                try:
                    server_reader, server_writer = await asyncio.open_connection(host=self.config['host'],
                                                                                 port=self.config['port'],
                                                                                 ssl=self.context,
                                                                                 server_hostname=self.config['host'])
                    server_writer.write(self.config['uuid'])
                    await server_writer.drain()
                    self.connection_pool.append((server_reader, server_writer))
                except Exception as e:
                   traceback.clear_frames(e.__traceback__)
                   e.__traceback__ = None
            self.unhealthy = 0
            await asyncio.sleep(1)
            if len(self.connection_pool) + self.unhealthy < (pool_max_size / 2):
                pool_max_size *= 2

    async def pool_health(self):
        while True:
            for x in self.connection_pool:
                self.loop.create_task(self.check_health(x))
            for x in range(10):
                S = time.time()
                await asyncio.sleep(0.5)
                E = time.time()
                if E - S > 1.5:
                    break

    async def check_health(self, x):
        try:
            x[1].write(int.to_bytes(0, 2, 'big', signed=True))
            await x[1].drain()
        except Exception as e:
            traceback.clear_frames(e.__traceback__)
            e.__traceback__ = None
            self.connection_pool.remove(x)
            self.unhealthy += 1
            await self.clean_up(x[0], x[1])

    async def update_white_list(self):
        while True:
            try:
                server_writer = None
                file = None
                server_reader, server_writer = await asyncio.open_connection(host=self.config['host'],
                                                                             port=self.config['port'],
                                                                             ssl=self.context,
                                                                             server_hostname=self.config['host'])
                server_writer.write(self.config['uuid'])
                await server_writer.drain()
                server_writer.write(int.to_bytes(-3, 2, 'big', signed=True))
                await server_writer.drain()
                customize = b''
                while True:
                    data = await server_reader.read(8192)
                    if data == b'' or data == b'\n':
                        break
                    customize += data
                if os.path.exists(self.config['white_list']) and customize != b'':
                    with open(self.config['white_list'], 'r') as file:
                        data = json.load(file)
                    customize = json.loads(gzip.decompress(customize))
                    data += customize
                    for x in list(map(self.encode, customize)):
                        self.white_list.add(x.replace(b'*', b''))
                    data = list(set(data))
                    with open(self.config['white_list'], 'w') as file:
                        json.dump(data, file)
                elif customize != b'':
                    with open(self.config['white_list'], 'wb') as file:
                        file.write(customize)
                await self.clean_up(server_writer, file)
            except Exception as e:
                traceback.clear_frames(e.__traceback__)
                e.__traceback__ = None
                await self.clean_up(server_writer, file)
            await asyncio.sleep(60)

    def exception_handler(self, loop, context):
        pass

    async def process(self, data, client_reader, client_writer):
        request_type = self.get_request_type(data)
        if request_type == 3:
            host, port = await self.get_socks5_address(client_reader, client_writer)
            data = None
            URL = None
        elif request_type == 0:
            URL, host, port = self.get_http_address(data, request_type)
            data = None
        else:
            URL, host, port = self.get_http_address(data, request_type)
            data = self.get_response(data, host, port)
        return data, URL, host, port, request_type

    def get_type(self, host):
        if self.config['mode'] == 'global':
            return True
        elif self.config['mode'] == 'auto':
            if self.in_it(host, self.black_list):
                return True
            elif not self.in_it(host, self.white_list):
                return True
        return False

    def get_request_type(self, data):
        if data[:7] == b'CONNECT':
            request_type = 0
        elif data[:3] == b'GET':
            request_type = 1
        elif data[:4] == b'POST':
            request_type = 2
        else:
            request_type = 3
        return request_type

    def get_http_address(self, data, request_type):
        position = data.find(b' ') + 1
        sigment = data[position:data.find(b' ', position)]
        if request_type:
            URL = sigment.replace(b'http', b'https', 1)
        else:
            URL = None
        if request_type:
            position = sigment.find(b'//') + 2
            sigment = sigment[position:sigment.find(b'/', position)]
        position = sigment.rfind(b':')
        if position > 0 and position > sigment.rfind(b']'):
            host = sigment[:position]
            port = sigment[position + 1:]
        else:
            host = sigment
            port = b'80'
        host = host.replace(b'[', b'', 1)
        host = host.replace(b']', b'', 1)
        return URL, host, port

    async def get_socks5_address(self, client_reader, client_writer):
        client_writer.write(b'\x05\x00')
        await client_writer.drain()
        data = await asyncio.wait_for(client_reader.read(65535), 20)
        if data[3] == 1:
            host = socket.inet_ntop(socket.AF_INET, data[4:8]).encode('utf-8')
            port = str(int.from_bytes(data[-2:], 'big')).encode('utf-8')
        elif data[3] == 4:
            host = socket.inet_ntop(socket.AF_INET6, data[4:20]).encode('utf-8')
            port = str(int.from_bytes(data[-2:], 'big')).encode('utf-8')
        elif data[3] == 3:
            host = data[5:5 + data[4]]
            port = str(int.from_bytes(data[-2:], 'big')).encode('utf-8')
        client_writer.write(b'\x05\x00\x00' + data[3:])
        await client_writer.drain()
        return host, port

    def get_response(self, data, host, port):
        data = data.replace(b'http://', b'', 1)
        data = data.replace(host, b'', 1)
        data = data.replace(b':' + port, b'', 1)
        data = data.replace(b'[]', b'', 1)
        data = data.replace(b'Proxy-', b'', 1)
        return data

    def in_it(self, host, var):
        if host in var:
            return True
        sigment_length = len(host)
        while True:
            sigment_length = host.rfind(b'.', 0, sigment_length) - 1
            if sigment_length <= -1:
                break
            if host[sigment_length + 1:] in var:
                return True
        if host[0] == 49:
            try:
                ip = int(ipaddress.ip_address(host.decode('utf-8')))
                for x in self.local_ip_list:
                    if x[0] <= ip and ip <= x[1]:
                        return True
            except Exception as e:
                traceback.clear_frames(e.__traceback__)
                e.__traceback__ = None
        return False

    def get_context(self):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS)
        context.minimum_version = ssl.TLSVersion.TLSv1_3
        context.set_alpn_protocols(['h2', 'http/1.1'])
        context.verify_mode = ssl.CERT_REQUIRED
        context.check_hostname = True
        context.load_verify_locations(self.config_path + self.config['cert'])
        return context


class yashmak(core):
    def __init__(self):
        self.white_list = set()
        self.black_list = set()
        self.HSTS_list = set()
        self.local_ip_list = []
        self.load_config()
        self.set_proxy()
        self.load_exception_list()
        self.write_pid()

    def serve_forever(self):
        core.__init__(self)

    def load_config(self):
        self.config_path = os.path.abspath(os.path.dirname(sys.argv[0])) + '/Config/'
        if os.path.exists(self.config_path + 'config.json'):
            with open(self.config_path + 'config.json', 'r') as file:
                content = file.read()
            content = self.translate(content)
            self.config = json.loads(content)
            self.config[self.config['active']]['mode'] = self.config['mode']
            self.config[self.config['active']]['white_list'] = self.config_path + self.config['white_list']
            self.config[self.config['active']]['black_list'] = self.config_path + self.config['black_list']
            self.config[self.config['active']]['HSTS_list'] = self.config_path + self.config['HSTS_list']
            self.config = self.config[self.config['active']]
            self.config['uuid'] = self.config['uuid'].encode('utf-8')
            self.config['listen'] = int(self.config['listen'])
        else:
            example = {'mode': '', 'active': '', 'white_list': '', 'black_list': '', 'HSTS_list': '',
                       'server01': {'cert': '', 'host': '', 'port': '', 'uuid': '', 'listen': ''}}
            with open(self.config_path + 'config.json', 'w') as file:
                json.dump(example, file, indent=4)

    def load_exception_list(self):
        def load_list(location, var, func):
            if location != '':
                with open(location, 'r') as file:
                    data = json.load(file)
                data = list(map(func, data))
                for x in data:
                    var.add(x.replace(b'*', b''))

        load_list(self.config['white_list'], self.white_list, self.encode)
        load_list(self.config['black_list'], self.black_list, self.encode)
        load_list(self.config['HSTS_list'], self.HSTS_list, self.encode)
        for x in ['192.168.0.0/16','172.16.0.0/12','10.0.0.0/8']:
            network = ipaddress.ip_network(x)
            self.local_ip_list.append([int(network[0]), int(network[-1])])
        self.local_ip_list.sort()

    def set_proxy(self):
        platform = sys.platform
        if platform == 'win32':
            os.popen('''reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyEnable /t REG_DWORD /d 1 /f''')
            os.popen('''reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyServer /d "127.0.0.1:'''+str(self.config['listen'])+'''" /f''')
            os.popen('''reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings" /v ProxyOverride /d "localhost;127.*;10.*;172.16.*;172.17.*;172.18.*;172.19.*;172.20.*;172.21.*;172.22.*;172.23.*;172.24.*;172.25.*;172.26.*;172.27.*;172.28.*;172.29.*;172.30.*;172.31.*;172.32.*;192.168.*;windows10.microdone.cn;<local>" /f''')
        elif platform == 'darwin':
            os.popen('''networksetup -setwebproxystate "Wi-Fi" on''')
            os.popen('''networksetup -setsecurewebproxystate "Wi-Fi" on''')
            os.popen('''networksetup -setwebproxy "Wi-Fi" 127.0.0.1 '''+str(self.config['listen']))
            os.popen('''networksetup -setsecurewebproxy "Wi-Fi" 127.0.0.1 '''+str(self.config['listen']))
            os.popen('''networksetup -setproxybypassdomains "Wi-Fi" localhost 127.* 10.* 172.16.* 172.17.* 172.18.* 172.19.* 172.20.* 172.21.* 172.22.* 172.23.* 172.24.* 172.25.* 172.26.* 172.27.* 172.28.* 172.29.* 172.30.* 172.31.* 172.32.* 192.168.*''')
            os.popen('''networksetup -setwebproxystate "Ethernet" on''')
            os.popen('''networksetup -setsecurewebproxystate "Ethernet" on''')
            os.popen('''networksetup -setwebproxy "Ethernet" 127.0.0.1 '''+str(self.config['listen']))
            os.popen('''networksetup -setsecurewebproxy "Ethernet" 127.0.0.1 '''+str(self.config['listen']))
            os.popen('''networksetup -setproxybypassdomains "Ethernet" localhost 127.* 10.* 172.16.* 172.17.* 172.18.* 172.19.* 172.20.* 172.21.* 172.22.* 172.23.* 172.24.* 172.25.* 172.26.* 172.27.* 172.28.* 172.29.* 172.30.* 172.31.* 172.32.* 192.168.*''')

    def write_pid(self):
        with open(self.config_path + 'pid','w') as file:
            file.write(str(os.getpid()))

    def translate(self, content):
        return content.replace('\\', '/')

    def encode(self, data):
        return data.encode('utf-8')

if __name__ == '__main__':
    server = yashmak()
    server.serve_forever()