# Klippy WebHooks registration and server connection
#
# Copyright (C) 2020 Eric Callahan <arksine.code@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license
import logging
import socket
import os
import sys
import errno
import json
import homing

# Json decodes strings as unicode types in Python 2.x.  This doesn't
# play well with some parts of Klipper (particuarly displays), so we
# need to create an object hook. This solution borrowed from:
#
# https://stackoverflow.com/questions/956867/
#
def byteify(data, ignore_dicts=False):
    if isinstance(data, unicode):
        return data.encode('utf-8')
    if isinstance(data, list):
        return [byteify(i, True) for i in data]
    if isinstance(data, dict) and not ignore_dicts:
        return {byteify(k, True): byteify(v, True)
                for k, v in data.items()}
    return data

def json_loads_byteified(data):
    return byteify(
        json.loads(data, object_hook=byteify), True)

class WebRequestError(homing.CommandError):
    def __init__(self, message,):
        Exception.__init__(self, message)

    def to_dict(self):
        return {
            'error': 'WebRequestError',
            'message': self.message}

class Sentinel:
    pass

class WebRequest:
    error = WebRequestError
    def __init__(self, client_conn, base_request):
        self.client_conn = client_conn
        self.id = base_request['id']
        self.path = base_request['path']
        self.args = base_request['args']
        self.response = None

    def get_client_connection(self):
        return self.client_conn

    def get(self, item, default=Sentinel):
        if item not in self.args:
            if default == Sentinel:
                raise WebRequestError("Invalid Argument [%s]" % item)
            return default
        return self.args[item]

    def get_int(self, item):
        return int(self.get(item))

    def get_float(self, item):
        return float(self.get(item))

    def get_args(self):
        return self.args

    def get_path(self):
        return self.path

    def set_error(self, error):
        self.response = error.to_dict()

    def send(self, data):
        if self.response is not None:
            raise WebRequestError("Multiple calls to send not allowed")
        self.response = data

    def finish(self):
        if self.response is None:
            # No error was set and the user never executed
            # send, default response is "ok"
            self.response = "ok"
        return {"request_id": self.id, "response": self.response}

class ServerSocket:
    def __init__(self, webhooks, printer):
        self.printer = printer
        self.webhooks = webhooks
        self.reactor = printer.get_reactor()
        self.sock = self.fd_handle = None
        self.clients = {}
        start_args = printer.get_start_args()
        server_address = start_args.get('apiserver')
        is_fileinput = (start_args.get('debuginput') is not None)
        if not server_address or is_fileinput:
            # Do not enable server
            return
        self._remove_socket_file(server_address)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.setblocking(0)
        self.sock.bind(server_address)
        self.sock.listen(1)
        self.fd_handle = self.reactor.register_fd(
            self.sock.fileno(), self._handle_accept)
        printer.register_event_handler(
            'klippy:disconnect', self._handle_disconnect)

    def _handle_accept(self, eventtime):
        try:
            sock, addr = self.sock.accept()
        except socket.error:
            return
        sock.setblocking(0)
        client = ClientConnection(self, sock)
        self.clients[client.uid] = client

    def _handle_disconnect(self):
        for client in list(self.clients.values()):
            client.close()
        if self.sock is not None:
            self.reactor.unregister_fd(self.fd_handle)
            try:
                self.sock.close()
            except socket.error:
                pass

    def _remove_socket_file(self, file_path):
        try:
            os.remove(file_path)
        except OSError:
            if os.path.exists(file_path):
                logging.exception(
                    "webhooks: Unable to delete socket file '%s'"
                    % (file_path))
                raise

    def pop_client(self, client_id):
        self.clients.pop(client_id, None)

class ClientConnection:
    def __init__(self, server, sock):
        self.printer = server.printer
        self.webhooks = server.webhooks
        self.reactor = server.reactor
        self.server = server
        self.uid = id(self)
        self.sock = sock
        self.fd_handle = self.reactor.register_fd(
            self.sock.fileno(), self.process_received)
        self.partial_data = self.send_buffer = ""
        self.is_sending_data = False
        logging.info(
            "webhooks: New connection established")

    def close(self):
        if self.fd_handle is not None:
            logging.info("webhooks: Client connection closed")
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
            try:
                self.sock.close()
            except socket.error:
                pass
            self.server.pop_client(self.uid)

    def is_closed(self):
        return self.fd_handle is None

    def process_received(self, eventtime):
        try:
            data = self.sock.recv(4096)
        except socket.error as e:
            # If bad file descriptor allow connection to be
            # closed by the data check
            if e.errno == errno.EBADF:
                data = ''
            else:
                return
        if data == '':
            # Socket Closed
            self.close()
            return
        requests = data.split('\x03')
        requests[0] = self.partial_data + requests[0]
        self.partial_data = requests.pop()
        for req in requests:
            logging.debug(
                "webhooks: Request received: %s" % (req))
            try:
                web_request = WebRequest(self, json_loads_byteified(req))
            except Exception:
                logging.exception(
                    "webhooks: Error decoding Server Request %s"
                    % (req))
                continue
            self.reactor.register_callback(
                lambda e, s=self, wr=web_request: s._process_request(wr))

    def _process_request(self, web_request):
        try:
            func = self.webhooks.get_callback(
                web_request.get_path())
            func(web_request)
        except homing.CommandError as e:
            web_request.set_error(WebRequestError(e.message))
        except Exception as e:
            msg = "Internal Error on WebRequest: %s" % (web_request.get_path())
            logging.exception(msg)
            web_request.set_error(WebRequestError(e.message))
            self.printer.invoke_shutdown(msg)
        result = web_request.finish()
        logging.debug(
            "webhooks: Sending response - %s" % (str(result)))
        self.send({'method': "response", 'params': result})

    def send(self, data):
        self.send_buffer += json.dumps(data) + "\x03"
        if not self.is_sending_data:
            self.is_sending_data = True
            self.reactor.register_callback(self._do_send)

    def _do_send(self, eventtime):
        retries = 10
        while self.send_buffer:
            try:
                sent = self.sock.send(self.send_buffer)
            except socket.error as e:
                if e.errno == errno.EBADF or e.errno == errno.EPIPE \
                        or not retries:
                    sent = 0
                else:
                    retries -= 1
                    waketime = self.reactor.monotonic() + .001
                    self.reactor.pause(waketime)
                    continue
            retries = 10
            if sent > 0:
                self.send_buffer = self.send_buffer[sent:]
            else:
                logging.info(
                    "webhooks: Error sending server data,  closing socket")
                self.close()
                break
        self.is_sending_data = False

class WebHooks:
    def __init__(self, printer):
        self.printer = printer
        self._endpoints = {"list_endpoints": self._handle_list_endpoints}
        self.register_endpoint("info", self._handle_info_request)
        self.register_endpoint("emergency_stop", self._handle_estop_request)
        self.sconn = ServerSocket(self, printer)

    def register_endpoint(self, path, callback):
        if path in self._endpoints:
            raise WebRequestError("Path already registered to an endpoint")
        self._endpoints[path] = callback

    def _handle_list_endpoints(self, web_request):
        web_request.send({'endpoints': self._endpoints.keys()})

    def _handle_info_request(self, web_request):
        state_message, state = self.printer.get_state_message()
        klipper_path = os.path.normpath(os.path.join(
            os.path.dirname(__file__), ".."))
        response = {'state': state, 'state_message': state_message,
                    'hostname': socket.gethostname(),
                    'klipper_path': klipper_path, 'python_path': sys.executable}
        start_args = self.printer.get_start_args()
        for sa in ['log_file', 'config_file', 'software_version', 'cpu_info']:
            response[sa] = start_args.get(sa)
        web_request.send(response)

    def _handle_estop_request(self, web_request):
        self.printer.invoke_shutdown("Shutdown due to webhooks request")

    def get_connection(self):
        return self.sconn

    def get_callback(self, path):
        cb = self._endpoints.get(path, None)
        if cb is None:
            msg = "webhooks: No registered callback for path '%s'" % (path)
            logging.info(msg)
            raise WebRequestError(msg)
        return cb

    def get_status(self, eventtime):
        state_message, state = self.printer.get_state_message()
        return {'state': state, 'state_message': state_message}

class GCodeOutputHelper:
    def __init__(self, printer):
        self.printer = printer
        wh = printer.lookup_object('webhooks')
        wh.register_endpoint("subscribe_gcode_output",
                               self._handle_subscribe_gcode_output)
        self.is_registered = False
        self.clients = {}
    def _gcode_callback(self, msg):
        for cconn, template in list(self.clients.items()):
            if cconn.is_closed():
                del self.clients[cconn]
                continue
            tmp = dict(template)
            tmp['params'] = {'response': msg}
            cconn.send(tmp)
    def _handle_subscribe_gcode_output(self, web_request):
        cconn = web_request.get_client_connection()
        template = web_request.get('response_template', {})
        self.clients[cconn] = template
        if not self.is_registered:
            gcode = self.printer.lookup_object('gcode')
            gcode.register_output_handler(self._gcode_callback)
            self.is_registered = True

SUBSCRIPTION_REFRESH_TIME = .25

class StatusHandler:
    def __init__(self, printer):
        self.printer = printer
        self.webhooks = webhooks = printer.lookup_object('webhooks')
        self.ready = self.timer_started = False
        self.reactor = self.printer.get_reactor()
        self.available_objects = {}
        self.subscriptions = {}
        self.subscription_timer = self.reactor.register_timer(
            self._batch_subscription_handler, self.reactor.NEVER)
        self.clients = {}

        # Register events
        self.printer.register_event_handler(
            "klippy:ready", self._handle_ready)
        self.printer.register_event_handler(
            "gcode:request_restart", self._handle_restart)

        # Register webhooks
        webhooks.register_endpoint("objects/list", self._handle_object_request)
        webhooks.register_endpoint("objects/status",
                                   self._handle_status_request)
        webhooks.register_endpoint("objects/subscription",
                                   self._handle_subscription_request)
        webhooks.register_endpoint("objects/list_subscription",
                                   self._handle_list_subscription_request)

    def _handle_ready(self):
        eventtime = self.reactor.monotonic()
        self.available_objects = {}
        objs = self.printer.lookup_objects()
        status_objs = {n: o for n, o in objs if hasattr(o, "get_status")}
        for name, obj in status_objs.items():
            attrs = obj.get_status(eventtime)
            self.available_objects[name] = attrs.keys()
        self.ready = True

    def _handle_restart(self, eventtime):
        self.ready = False
        self.reactor.update_timer(self.subscription_timer, self.reactor.NEVER)

    def _batch_subscription_handler(self, eventtime):
        status = self._process_status_request(self.subscriptions, eventtime)
        for cconn, template in list(self.clients.items()):
            if cconn.is_closed():
                del self.clients[cconn]
                continue
            tmp = dict(template)
            tmp['params'] = {'status': status}
            cconn.send(tmp)
        return eventtime + SUBSCRIPTION_REFRESH_TIME

    def _process_status_request(self, requested_objects, eventtime):
        result = {}
        if self.ready:
            for name, req_items in requested_objects.items():
                obj = self.printer.lookup_object(name, None)
                if obj is not None and name in self.available_objects:
                    status = obj.get_status(eventtime)
                    if not req_items:
                        # return all items excluding callables
                        result[name] = {k: v for k, v in status.items()
                                        if not callable(v)}
                    else:
                        # return requested items excluding callables
                        result[name] = {k: v for k, v in status.items()
                                        if k in req_items and not callable(v)}
        else:
            result = {"status": "Klippy Not Ready"}
        return result

    def _handle_object_request(self, web_request):
        web_request.send(dict(self.available_objects))

    def _handle_status_request(self, web_request):
        args = web_request.get_args()
        eventtime = self.reactor.monotonic()
        result = self._process_status_request(args, eventtime)
        web_request.send(result)

    def _handle_subscription_request(self, web_request):
        # XXX - should allow per-client subscription lists
        args = web_request.get_args()
        if not args:
            raise web_request.error("Invalid argument")
        self.add_subscripton(args)
        cconn = web_request.get_client_connection()
        template = web_request.get('response_template', {})
        self.clients[cconn] = template

    def _handle_list_subscription_request(self, web_request):
        web_request.send(dict(self.subscriptions))

    def add_subscripton(self, new_sub):
        if not new_sub:
            return
        for obj_name, req_items in new_sub.items():
            if obj_name not in self.available_objects:
                logging.info(
                    "webhooks: Object {%s} not available for subscription"
                    % (obj_name))
                continue
            # validate requested items
            if req_items:
                avail_items = set(self.available_objects[obj_name])
                invalid_items = set(req_items) - avail_items
                if invalid_items:
                    logging.info(
                        "webhooks: Removed invalid items [%s] from "
                        "subscription request %s" %
                        (", ".join(invalid_items), obj_name))
                    req_items = list(set(req_items) - invalid_items)
                    if not req_items:
                        # No valid items remaining
                        continue
            # Add or update subscription
            existing_items = self.subscriptions.get(obj_name, None)
            if existing_items is not None:
                if req_items == [] or existing_items == []:
                    # Subscribe to all items
                    self.subscriptions[obj_name] = []
                else:
                    req_items = list(set(req_items) | set(existing_items))
                    self.subscriptions[obj_name] = req_items
            else:
                self.subscriptions[obj_name] = req_items
        if not self.timer_started:
            self.reactor.update_timer(self.subscription_timer, self.reactor.NOW)
            self.timer_started = True

def add_early_printer_objects(printer):
    printer.add_object('webhooks', WebHooks(printer))
    GCodeOutputHelper(printer)
    StatusHandler(printer)
