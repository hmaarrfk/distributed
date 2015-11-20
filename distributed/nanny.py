from __future__ import print_function, division, absolute_import

import logging
from multiprocessing import Process, Queue, queues

from tornado.ioloop import IOLoop
from tornado import gen

from .core import Server, rpc


logger = logging.getLogger(__name__)

class Nanny(Server):
    def __init__(self, ip, port, worker_port, center_ip, center_port,
                ncores=None, loop=None, **kwargs):
        self.ip = ip
        self.port = port
        self.worker_port = worker_port
        self.ncores = ncores
        self.status = None
        self.process = None
        self.loop = loop or IOLoop.current()
        self.center = rpc(ip=center_ip, port=center_port)

        handlers = {'instantiate': self._instantiate,
                    'kill': self._kill,
                    'terminate': self.terminate}

        super(Nanny, self).__init__(handlers, **kwargs)

    @gen.coroutine
    def _start(self):
        self.listen(self.port)
        yield self._instantiate()
        self.loop.add_callback(self._watch)
        self.status = 'running'
        logger.info('Start Nanny at:             %s:%d',
                    self.center.ip, self.center.port)

    @gen.coroutine
    def _kill(self, stream=None, wait=True):
        if self.process:
            self.process.terminate()
            logger.info("Nanny %s:%d kills worker process %s:%d",
                        self.ip, self.port, self.ip, self.worker_port)
            result = yield self.center.unregister(address=self.worker_address)
            if result != b'OK':
                logger.critical("Unable to unregister with center. %s", result)
        self.process = None
        raise gen.Return(b'OK')

    @gen.coroutine
    def _instantiate(self, stream=None):
        q = Queue()
        self.process = Process(target=run_worker,
                               args=(q, self.ip, self.worker_port, self.center.ip,
                                     self.center.port, self.ncores,
                                     self.port))
        self.process.daemon = True
        self.process.start()
        logger.info("Nanny starts worker process %s:%d", self.ip, self.port)
        while True:
            try:
                self.worker_port = q.get_nowait()
                break
            except queues.Empty:
                yield gen.sleep(0.1)
        raise gen.Return(b'OK')

    @gen.coroutine
    def _watch(self, wait_seconds=0.10):
        while True:
            if self.status == 'closed':
                yield self._close()
                break
            if self.process and not self.process.is_alive():
                yield self.center.unregister(address=self.worker_address)
                yield self._instantiate()
            else:
                yield gen.sleep(wait_seconds)

    @gen.coroutine
    def _close(self):
        logger.info("Closing Nanny at %s:%d", self.ip, self.port)
        yield self._kill()
        self.center.close_streams()
        self.stop()
        self.status = 'closed'

    @gen.coroutine
    def terminate(self, stream=None):
        yield self._close()
        raise gen.Return(b'OK')

    @property
    def address(self):
        return (self.ip, self.port)

    @property
    def worker_address(self):
        return (self.ip, self.worker_port)


def run_worker(q, ip, port, center_ip, center_port, ncores, nanny_port):
    from distributed import Worker
    from tornado.ioloop import IOLoop
    IOLoop.clear_instance()
    loop = IOLoop(make_current=True)
    worker = Worker(ip, port, center_ip, center_port, ncores,
                    nanny_port=nanny_port)

    @gen.coroutine
    def start():
        yield worker._start()
        q.put(worker.port)
    loop.add_callback(start)
    loop.start()
