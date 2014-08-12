#!/usr/bin/env python
# -*- coding: utf-8 -*-
__author__ = 'moonshawdo@gamil.com'
"""
验证哪些IP可以用在gogagent中
主要是检查这个ip是否可以连通，并且证书是否为google.com
"""

import os
import sys
import threading
import socket
import ssl
import re
import select
import traceback
import logging
import random

PY3 = False
if sys.version_info[0] == 3:
    from queue import Queue, Empty
    PY3 = True
    try:
        from functools import reduce
    finally:
        pass
    try:
        xrange
    except NameError:
        xrange = range
else:
    from Queue import Queue, Empty
import time
from time import sleep
 
g_useOpenSSL = 1
g_usegevent = 1
if g_usegevent == 1:
    try:
        from gevent import monkey
        monkey.patch_all(Event=True)
        g_useOpenSSL = 0
        from gevent import sleep
    except ImportError:
        g_usegevent = 0

if g_useOpenSSL == 1:
    try:
        import OpenSSL.SSL

        SSLError = OpenSSL.SSL.WantReadError
        g_usegevent = 0
    except ImportError:
        g_useOpenSSL = 0
        SSLError = ssl.SSLError
else:
    SSLError = ssl.SSLError


"""
ip_str_list为需要查找的IP地址，第一组的格式：
1.xxx.xxx.xxx.xxx-xx.xxx.xxx.xxx
2.xxx.xxx.xxx.xxx/xx
3.xxx.xxx.xxx.
4 xxx.xxx.xxx.xxx

组与组之间可以用换行相隔开,第一行中IP段可以用'|'或','
获取随机IP是每组依次获取随机个数量的，因此一组的IP数越少，越有机会会检查，当然获取随机IP会先排除上次查询失败的IP
"""
ip_str_list = '''
218.189.25.166-218.189.25.187|121.78.74.80-121.78.74.88|178.45.251.84-178.45.251.123|210.61.221.148-210.61.221.187
61.219.131.84-61.219.131.251|202.39.143.84-202.39.143.123|203.66.124.148-203.66.124.251|203.211.0.20-203.211.0.59
60.199.175.18-60.199.175.187|218.176.242.20-218.176.242.251|203.116.165.148-203.116.165.251|203.117.34.148-203.117.34.187
210.153.73.20-210.153.73.123|106.162.192.148-106.162.192.187|106.162.198.84-106.162.198.123|106.162.216.20-106.162.216.123
210.139.253.20-210.139.253.251|111.168.255.20-111.168.255.187|203.165.13.210-203.165.13.251
61.19.1.30-61.19.1.109|74.125.31.33-74.125.31.60|210.242.125.20-210.242.125.59|203.165.14.210-203.165.14.251
216.239.32.0/19
64.233.160.0/19
66.249.80.0/20
72.14.192.0/18
209.85.128.0/17
66.102.0.0/20
74.125.0.0/16
64.18.0.0/20
207.126.144.0/20
173.194.0.0/16
'''


#最大IP延时，单位毫秒
g_maxhandletimeout = 2000
#最大可用IP数量
g_maxhandleipcnt = 50

"连接超时设置"
g_conntimeout = 5
g_handshaketimeout = 7

g_filedir = os.path.dirname(__file__)
g_cacertfile = os.path.join(g_filedir, "cacert.pem")
g_ipfile = os.path.join(g_filedir, "ip.txt")
g_tmpokfile = os.path.join(g_filedir, "ip_tmpok.txt")
g_tmperrorfile = os.path.join(g_filedir, "ip_tmperror.txt")

g_maxthreads = 128

# gevent socket cnt must less than 1024
if g_usegevent == 1 and g_maxthreads > 1000:
    g_maxthreads = 128

g_ssldomain = ("google.com",)
g_excludessdomain=()


"是否自动删除记录查询成功的IP文件，0为不删除，1为删除"
"文件名：ip_tmpok.txt，格式：ip 连接与握手时间 ssl域名"
g_autodeltmpokfile = 1
"是否自动删除记录查询失败的IP文件，0为不删除，1为删除"
"ip_tmperror.txt，格式：ip"
g_autodeltmperrorfile = 0

logging.basicConfig(format="[%(threadName)s]%(message)s",level=logging.INFO)


evt_ipramdomstart = threading.Event()
evt_ipramdomend = threading.Event()

def PRINT(strlog):
    logging.info(strlog)
    
def isgoolgledomain(domain):
    lowerdomain = domain.lower()
    if lowerdomain in g_ssldomain:
        return 1
    if lowerdomain in g_excludessdomain:
        return 0
    return 2

def isgoogleserver(svrname):
    lowerdomain = svrname.lower()
    if lowerdomain == "gws":
        return True
    else:
        return False

def checkvalidssldomain(domain,svrname):
    ret = isgoolgledomain(domain)
    if ret == 1:
        return True
    elif ret == 0:
        return False
    elif len(svrname) > 0 and isgoogleserver(svrname):
        return True
    else:
        return False

prekey="\nServer:"
def getgooglesvrnamefromheader(header):
    begin = header.find(prekey)
    if begin != -1: 
        begin += len(prekey)
        end = header.find("\n",begin)
        if end == -1:
            end = len(header)
        gws = header[begin:end].strip(" \t")
        return gws
    return ""

class TCacheResult(object):
    __slots__ = ["okqueue","failipqueue","oklock","errlock","okfile","errorfile","validipcnt"]
    def __init__(self):
        self.okqueue = Queue()
        self.failipqueue = Queue()
        self.oklock = threading.Lock()
        self.errlock = threading.Lock()
        self.okfile = None
        self.errorfile = None
        self.validipcnt = 0
    
    def addOKIP(self,costtime,ip,ssldomain,gwsname):
        bOK = False
        if checkvalidssldomain(ssldomain,gwsname):
            bOK = True
            self.okqueue.put((costtime,ip,ssldomain,gwsname))
        try:
            self.oklock.acquire()
            if self.okfile is None:
                self.okfile = open(g_tmpokfile,"a+",0)
            self.okfile.seek(0,2)
            line = "%s %d %s %s\n" % (ip, costtime, ssldomain,gwsname)
            self.okfile.write(line)
            if bOK and costtime <= g_maxhandletimeout:
                self.validipcnt += 1
                return self.validipcnt
            else:
                return 0
        finally:
            self.oklock.release()
            
    def addFailIP(self,ip):
        try:
            self.errlock.acquire()
            if self.errorfile is None:
                self.errorfile = open(g_tmperrorfile,"a+",0)
            self.errorfile.seek(0,2)
            self.errorfile.write(ip+"\n")
            self.failipqueue.put(ip)
            if self.failipqueue.qsize() > 128:
                self.flushFailIP()
        finally:
            self.errlock.release() 
    
    def close(self):
        if self.okfile:
            self.okfile.close()
            self.okfile = None
        if self.errorfile:
            self.errorfile.close()
            self.errorfile = None
       
    def getIPResult(self):
        return self._queuetolist(self.okqueue)
        
    def _queuetolist(self,myqueue):
        result = []
        try:
            qsize = myqueue.qsize()
            while qsize > 0:
                result.append(myqueue.get_nowait())
                myqueue.task_done()
                qsize -= 1
        except Empty:
            pass
        return result

    def _cleanqueue(self,myqueue):
        try:
            qsize = myqueue.qsize()
            while qsize > 0:
                myqueue.get_nowait()
                myqueue.task_done()
                qsize -= 1
        except Empty:
            pass
    
    def flushFailIP(self):
        if self.failipqueue.qsize() > 0 :
            qsize = self.failipqueue.qsize()
            self._cleanqueue(self.failipqueue)
            logging.info( str(qsize) + " ip timeout")


    def loadLastResult(self):
        okresult  = set()
        errorresult = set()
        if os.path.exists(g_tmpokfile):
            with open(g_tmpokfile,"r") as fd:
                for line in fd:
                    ips = line.strip("\r\n").split(" ")
                    if len(ips) < 3:
                        continue
                    gwsname = ""
                    if len(ips) > 3:
                        gwsname = ips[3]
                    okresult.add(from_string(ips[0]))
                    if checkvalidssldomain(ips[2],gwsname):
                        self.okqueue.put((int(ips[1]),ips[0],ips[2],gwsname))
        if os.path.exists(g_tmperrorfile):
            with open(g_tmperrorfile,"r") as fd:
                for line in fd:
                    ips = line.strip("\r\n").split(" ")
                    for item in ips:
                        errorresult.add(from_string(item))
        return okresult,errorresult
    
    def clearFile(self):
        self.close()
        if g_autodeltmpokfile and os.path.exists(g_tmpokfile):
            os.remove(g_tmpokfile)
            PRINT("remove file %s" % g_tmpokfile)
        if g_autodeltmperrorfile and os.path.exists(g_tmperrorfile):
            os.remove(g_tmperrorfile)
            PRINT("remove file %s" % g_tmperrorfile)
            
    def queryfinish(self):
        try:
            self.oklock.acquire()
            return self.validipcnt >= g_maxhandleipcnt
        finally:
            self.oklock.release()

class my_ssl_wrap(object):
    ssl_cxt = None
    ssl_cxt_lock = threading.Lock()
    httpreq = "GET / HTTP/1.1\r\nAccept: */*\r\nHost: %s\r\nConnection: Keep-Alive\r\n\r\n"

    def __init__(self):
        pass

    @staticmethod
    def initsslcxt():
        if my_ssl_wrap.ssl_cxt is not None:
            return
        try:
            my_ssl_wrap.ssl_cxt_lock.acquire()
            if my_ssl_wrap.ssl_cxt is not None:
                return
            my_ssl_wrap.ssl_cxt = OpenSSL.SSL.Context(OpenSSL.SSL.TLSv1_METHOD)
            my_ssl_wrap.ssl_cxt.set_timeout(g_handshaketimeout)
            PRINT("init ssl context ok")
        except Exception:
            raise
        finally:
            my_ssl_wrap.ssl_cxt_lock.release()

    def getssldomain(self, threadname, ip):
        time_begin = time.time()
        s = None
        c = None
        haserror = 1
        timeout = 0
        domain = None
        gwsname = ""
        try:
            s = socket.socket()
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if g_useOpenSSL:
                my_ssl_wrap.initsslcxt()
                s.settimeout(g_conntimeout)
                s.connect((ip, 443))
                c = OpenSSL.SSL.Connection(my_ssl_wrap.ssl_cxt, s)
                c.set_connect_state()
                s.setblocking(0)
                while True:
                    try:
                        c.do_handshake()
                        break
                    except SSLError:
                        infds, outfds, errfds = select.select([s, ], [], [], g_handshaketimeout)
                        if len(infds) == 0:
                            raise SSLError("do_handshake timed out")
                        else:
                            costtime = int(time.time() - time_begin)
                            if costtime > g_handshaketimeout:
                                raise SSLError("do_handshake timed out")
                            else:
                                pass
                    except OpenSSL.SSL.SysCallError as e:
                        raise SSLError(e.args)
                cert = c.get_peer_certificate()
                time_end = time.time()
                costtime = int(time_end * 1000 - time_begin * 1000)
                for subject in cert.get_subject().get_components():
                    if subject[0] == "CN":
                        domain = subject[1]
                        haserror = 0
                if domain is None:
                    PRINT("%s can not get CN: %s " % (ip, cert.get_subject().get_components()))
                #尝试发送http请求，获取回应头部的Server字段
                if domain is None or isgoolgledomain(domain) == 2:
                    cur_time = time.time()
                    gwsname = self.getgooglesvrname(c,s,ip)
                    time_end = time.time()
                    costtime += int(time_end * 1000 - cur_time * 1000)
                    if domain is None and len(gwsname) > 0:
                        domain="defaultgws"
                return domain, costtime,timeout,gwsname
            else:
                s.settimeout(g_conntimeout)
                c = ssl.wrap_socket(s, cert_reqs=ssl.CERT_REQUIRED, ca_certs=g_cacertfile,
                                    do_handshake_on_connect=False)
                c.settimeout(g_conntimeout)
                c.connect((ip, 443))
                c.settimeout(g_handshaketimeout)
                c.do_handshake()
                cert = c.getpeercert()
                time_end = time.time()
                costtime = int(time_end * 1000 - time_begin * 1000)
                if 'subject' in cert:
                    subjectitems = cert['subject']
                    for mysets in subjectitems:
                        for item in mysets:
                            if item[0] == "commonName":
                                if not isinstance(item[1], str):
                                    domain = item[1].encode("utf-8")
                                else:
                                    domain = item[1]
                                haserror = 0
                    if domain is None:
                        PRINT("%s can not get commonName: %s " % (ip, subjectitems))
                #尝试发送http请求，获取回应头部的Server字段
                if domain is None or isgoolgledomain(domain) == 2:
                    cur_time = time.time()
                    gwsname = self.getgooglesvrname(c,s,ip)
                    time_end = time.time()
                    costtime += int(time_end * 1000 - cur_time * 1000)
                    if domain is None and len(gwsname) > 0:
                        domain="defaultgws"
                return domain, costtime,timeout,gwsname
        except SSLError as e:
            time_end = time.time()
            costtime = int(time_end * 1000 - time_begin * 1000)
            if str(e).endswith("timed out"):
                timeout = 1
            else:
                PRINT("SSL Exception(%s): %s, times:%d ms " % (ip, e, costtime))
            return domain, costtime,timeout,gwsname
        except IOError as e:
            time_end = time.time()
            costtime = int(time_end * 1000 - time_begin * 1000)
            if str(e).endswith("timed out"):
                timeout = 1
            else:
                PRINT("Catch IO Exception(%s): %s, times:%d ms " % (ip, e, costtime))
            return domain, costtime,timeout,gwsname
        except Exception as e:
            time_end = time.time()
            costtime = int(time_end * 1000 - time_begin * 1000)
            PRINT("Catch Exception(%s): %s, times:%d ms " % (ip, e, costtime))
            return domain, costtime,timeout,gwsname
        finally:
            if g_useOpenSSL:
                if c:
                    if haserror == 0:
                        c.shutdown()
                        c.sock_shutdown(2)
                    c.close()
                if s:
                    s.close()
            else:
                if c:
                    if haserror == 0:
                        c.shutdown(2)
                    c.close()
                elif s:
                    s.close()
                    
    def getgooglesvrname(self,conn,sock,ip):
        try:
            myreq = my_ssl_wrap.httpreq % ip
            conn.write(myreq)
            data=""
            sock.setblocking(0)
            trycnt = 0
            begin = time.time()
            conntimeout = g_conntimeout if g_usegevent == 0 else 0.001
            while True:
                end = time.time()
                costime = int(end-begin)
                if costime >= g_conntimeout:
                    PRINT("get http response timeout(%ss),ip:%s,cnt:%d" % (costime,ip,trycnt) )
                    return ""
                trycnt += 1
                infds, outfds, errfds = select.select([sock, ], [], [], conntimeout)
                if len(infds) == 0:
                    if g_usegevent == 1:
                        sleep(0.5)
                    continue
                timeout = 0
                try:
                    d = conn.read(1024)
                except SSLError as e:
                    sleep(0.5)
                    continue
                data = data + d.replace("\r","")
                index = data.find("\n\n")
                if index != -1:
                    gwsname = getgooglesvrnamefromheader(data[0:index])
                    return gwsname
            return ""
        except Exception as e:
            info = "%s" % e
            if len(info) == 0:
                info = type(e)
            PRINT("Catch Exception(%s) in getgooglesvrname: %s" % (ip, info))
            return ""


class Ping(threading.Thread):
    ncount = 0
    ncount_lock = threading.Lock()
    __slots__=["checkqueue","cacheResult"]
    def __init__(self,checkqueue,cacheResult):
        threading.Thread.__init__(self)
        self.queue = checkqueue
        self.cacheResult = cacheResult

    def runJob(self):
        while not evt_ipramdomstart.is_set():
            evt_ipramdomstart.wait(5)
        while not self.cacheResult.queryfinish():
            try:
                if self.queue.qsize() == 0 and evt_ipramdomend.is_set():
                    break
                addrint = self.queue.get(True,2)
                ipaddr = to_string(addrint)
                self.queue.task_done()
                ssl_obj = my_ssl_wrap()
                (ssldomain, costtime,timeout,gwsname) = ssl_obj.getssldomain(self.getName(), ipaddr)
                if ssldomain is not None:
                    cnt = self.cacheResult.addOKIP(costtime, ipaddr, ssldomain,gwsname)
                    if cnt != 0:
                        PRINT("ip: %s,CN: %s,svr: %s,ok:1,cnt:%d" % (ipaddr, ssldomain,gwsname,cnt))
                    else:
                        PRINT("ip: %s,CN: %s,svr: %s,ok:0" % (ipaddr, ssldomain,gwsname))
                elif ssldomain is None:
                    self.cacheResult.addFailIP(ipaddr)
            except Empty:
                pass

    def run(self):
        try:
            Ping.ncount_lock.acquire()
            Ping.ncount += 1
            Ping.ncount_lock.release()
            self.runJob()
        except Exception:
            raise
        finally:
            Ping.ncount_lock.acquire()
            Ping.ncount -= 1
            Ping.ncount_lock.release()
    
    @staticmethod 
    def getCount():
        try:
            Ping.ncount_lock.acquire()
            return Ping.ncount
        finally:
            Ping.ncount_lock.release()
            
            
class RamdomIP(threading.Thread):
    def __init__(self,checkqueue,cacheResult):
        threading.Thread.__init__(self)
        self.ipqueue = checkqueue
        self.cacheResult = cacheResult
        
    def ramdomip(self):
        lastokresult,lasterrorresult = self.cacheResult.loadLastResult()
        iplineslist = re.split("\r|\n", ip_str_list)
        skipokcnt = 0
        skiperrocnt = 0
        iplinelist = []
        totalipcnt = 0
        cacheip = lastokresult | lasterrorresult
        for iplines in iplineslist:
            if len(iplines) == 0 or iplines[0] == '#':
                continue
            singlelist = []
            ips = re.split(",|\|", iplines)
            for line in ips:
                if len(line) == 0 or line[0] == '#':
                    continue
                begin, end = splitip(line)
                if checkipvalid(begin) == 0 or checkipvalid(end) == 0:
                    PRINT("ip format is error,line:%s, begin: %s,end: %s" % (line, begin, end))
                    continue
                nbegin = from_string(begin)
                nend = from_string(end)
                iplinelist.append([nbegin,nend,nend - nbegin + 1])
        
        hadIPData = True
        putdata = False
        putipcnt = 0
        while hadIPData:
            if evt_ipramdomend.is_set():
                break
            hadIPData = False
            for itemlist in iplinelist:
                begin = itemlist[0]
                end = itemlist[1]
                itemlen = itemlist[2]
                if itemlen <= 0:
                    continue
                if self.cacheResult.queryfinish():
                    break
                if itemlen > 1000:
                    itemlen = 10
                elif itemlen > 5:
                    itemlen = 5
                if itemlen <= 2:
                    selectcnt = itemlen
                else:
                    selectcnt = random.randint(2,itemlen)
                for i in xrange(0,selectcnt):
                    k = random.randint(begin,end)
                    first = True
                    findOK = True
                    while k in cacheip:
                        if k < end:
                            k += 1
                        elif not first:
                            findOK = False
                            break
                        else:
                            first = False
                            k = begin
                    if findOK:
                        hadIPData = True
                        self.ipqueue.put(k)
                        cacheip.add(k)
                        putipcnt += 1
                        if not putdata:
                            evt_ipramdomstart.set()
                            putdata = True
                    if evt_ipramdomend.is_set():
                        break
                itemlist[2] -= i + 1
            if putipcnt >= 500:
                sleep(1)
                putipcnt = 0
        if not evt_ipramdomstart.is_set():
            evt_ipramdomstart.set()
        
    def run(self):
        PRINT("begin to get ramdom ip")
        self.ramdomip()
        evt_ipramdomend.set()
        PRINT("ramdom ip thread stopped.ip queue size: %d" % self.ipqueue.qsize())

def from_string(s):
    """Convert dotted IPv4 address to integer."""
    return reduce(lambda a, b: a << 8 | b, map(int, s.split(".")))


def to_string(ip):
    """Convert 32-bit integer to dotted IPv4 address."""
    return ".".join(map(lambda n: str(ip >> n & 0xFF), [24, 16, 8, 0]))


g_ipcheck = re.compile(r'^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$')


def checkipvalid(ip):
    """检查ipv4地址的合法性"""
    ret = g_ipcheck.match(ip)
    if ret is not None:
        "each item range: [0,255]"
        for item in ret.groups():
            if int(item) > 255:
                return 0
        return 1
    else:
        return 0


def splitip(strline):
    """从每组地址中分离出起始IP以及结束IP"""
    begin = ""
    end = ""
    if "-" in strline:
        "xxx.xxx.xxx.xxx-xxx.xxx.xxx.xxx"
        begin, end = strline.split("-")
    elif strline.endswith("."):
        "xxx.xxx.xxx."
        begin = strline + "0"
        end = strline + "255"
    elif "/" in strline:
        "xxx.xxx.xxx.xxx/xx"
        (ip, bits) = strline.split("/")
        if checkipvalid(ip) and (0 <= int(bits) <= 32):
            orgip = from_string(ip)
            end_bits = (1 << (32 - int(bits))) - 1
            begin_bits = 0xFFFFFFFF ^ end_bits
            begin = to_string(orgip & begin_bits)
            end = to_string(orgip | end_bits)
    else:
        "xxx.xxx.xxx.xxx"
        begin = strline
        end = strline

    return begin, end


def dumpstacks():
    code = []
    for threadId, stack in sys._current_frames().items():
        code.append("\n# Thread: %d" % (threadId))
        for filename, lineno, name, line in traceback.extract_stack(stack):
            code.append('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                code.append("  %s" % (line.strip()))
    PRINT("\n".join(code))
    
def checksingleprocess(ipqueue,cacheResult,max_threads):
    threadlist = []
    threading.stack_size(96 * 1024)
    PRINT('need create max threads count: %d' % (max_threads))
    for i in xrange(1, max_threads + 1):
        ping_thread = Ping(ipqueue,cacheResult)
        ping_thread.setDaemon(True)
        try:
            ping_thread.start()
        except threading.ThreadError as e:
            PRINT('start new thread except: %s,work thread cnt: %d' % (e, Ping.getCount()))
            break
        threadlist.append(ping_thread)
    try:
        for p in threadlist:
            p.join()
    except KeyboardInterrupt:
        evt_ipramdomend.set()
    cacheResult.close()
    

def list_ping():
    if g_useOpenSSL == 1:
        PRINT("support PyOpenSSL")
    if g_usegevent == 1:
        PRINT("support gevent")

    checkqueue = Queue()
    cacheResult = TCacheResult()
    lastokresult,lasterrorresult = cacheResult.loadLastResult()
    oklen = len(lastokresult)
    errorlen = len(lasterrorresult)
    totalcachelen = oklen + errorlen
    if totalcachelen != 0:
        PRINT("load last result,ok cnt:%d,error cnt: %d" % (oklen,errorlen) )
    
    ramdomip_thread = RamdomIP(checkqueue,cacheResult)
    ramdomip_thread.setDaemon(True)
    ramdomip_thread.start()
    checksingleprocess(checkqueue,cacheResult,g_maxthreads)
    
    cacheResult.flushFailIP()
    ip_list = cacheResult.getIPResult()
    ip_list.sort()

    PRINT('try to collect ssl result')
    op = 'wb'
    if sys.version_info[0] == 3:
        op = 'w'
    ff = open(g_ipfile, op)
    ncount = 0
    for ip in ip_list:
        domain = ip[2]
        if ip[0] > g_maxhandletimeout :
            break        
        PRINT("[%s] %d ms,domain: %s,svr:%s" % (ip[1], ip[0], domain,ip[3]))
        if domain is not None:
            ff.write(ip[1])
            ff.write("|")
            ncount += 1
    PRINT("write to file %s ok,count:%d " % (g_ipfile, ncount))
    ff.close()
    cacheResult.clearFile()


if __name__ == '__main__':
    list_ping()
