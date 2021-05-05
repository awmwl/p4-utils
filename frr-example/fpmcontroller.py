'''Socket programming code inspired by https://pythonprogramming.net/python-binding-listening-sockets/'''
'''PyRoute2 code inspired by  https://github.com/svinota/pyroute2 '''
''' Controller code inspired by nsg.eth.ch - p4learning'''

''' To run , use sudo python3 get_fpm.py <router-name> inside the router shell using mx <router-name> 
    after  sudo python3 run_network.py -t <topology-name> '''

import socket
import sys
from struct import *
import os
import multiprocessing

from importlib import import_module
from pyroute2.common import load_dump
from pyroute2.common import hexdump

from p4utils.utils.topology import NetworkGraph
from p4utils.utils.helper import load_topo
from p4utils.utils.sswitch_thrift_API import SimpleSwitchThriftAPI

class Controller(object):

    def __init__(self):

        self.topo = load_topo('topology.json')
        self.controllers = {}
        self.init()
        self.set_table_entries()
        
    # Initialization of controller for switches
    def init(self):
        self.connect_to_switches()
        self.set_table_defaults()
        
    # Connect to fake IP based thrift port on the switches to access it
    def connect_to_switches(self):
        for p4switch in self.topo.get_p4switches():
            
            thrift_port = self.topo.get_thrift_port(p4switch)
            num = sys.argv[1]

            # Check for router namespace which matches switch, can only connect to that switch from the router namespace
            if num[1] == p4switch[1]:
                self.controllers[p4switch] = SimpleSwitchThriftAPI(thrift_port, thrift_ip = "{}.0.0.{}".format(str(int(p4switch[1])*15), str(1)))

    def set_table_defaults(self):
        for controller in self.controllers.values():
            controller.table_set_default("ipv4_lpm", "drop", [])

    def set_table_entries(self):

        # get the FPM data for the Forwarding Information Base
        fib = self.main_fpm()
        print(fib)

        for sw_name, controller in self.controllers.items():
            

            for host in self.topo.get_hosts_connected_to(sw_name):

                host_ip = self.topo.get_host_ip(host) + "/24"
                host_mac = self.topo.get_host_mac(host)

                """ 
                Key to understand :
                List of routes installed by fpm
                position 0 selects the route (choose by looking at the FPM routes, which route to install)
                position 1 selects the IP match for lookup , tuple of the form ('RTA_DST', IP)
                position 2 selects the port to forward on , tuple of the form ('RTA_OIF', PORT) """

                # Connection to directly connected host
                host_ip_match_from_fib = fib[2][0][1] + "/24"
                port_toforward_from_fib = fib[2][1][1]
                
                # Need to match interface as PID number to real port
                if int(port_toforward_from_fib) > (int(fib[1][1][1]) or int(fib[3][1][1])):

                    # Port 1 is always connected to the host, port 3 to CP router, port 5 and 6 from CP fake interfaces, other ports to switches
                    sw_port = self.topo.node_to_node_port_num(sw_name, host)
                else:
                    sw_port = 2
                
                print()

                print ("table_add at {}:".format(sw_name))
                self.controllers[sw_name].table_add("ipv4_lpm", "set_nhop", [str(host_ip_match_from_fib)], [str(host_mac), str(sw_port)])

                # Connection to other switches 
                host_ip_match_from_fib = fib[1][0][1] + "/24"
                port_toforward_from_fib = fib[1][1][1]

                # Need to match interface as PID number to real port
                if int(port_toforward_from_fib) > (int(fib[1][1][1]) or int(fib[5][1][1])):
                    # Port 1 is always connected to the host, port 3 to CP router, port 5 and 6 from CP fake interfaces, other ports to switches
                    sw_port = self.topo.node_to_node_port_num(sw_name, host)
                else:
                    sw_port = 2

                print(sw_port)

                print ("table_add at {}:".format(sw_name))
                self.controllers[sw_name].table_add("ipv4_lpm", "set_nhop", [str(host_ip_match_from_fib)], [str(host_mac), str(sw_port)])

                host_ip_match_from_fib = fib[5][0][1] + "/24"
                port_toforward_from_fib = fib[5][1][1]

                print ("table_add at {}:".format(sw_name))
                self.controllers[sw_name].table_add("ipv4_lpm", "set_nhop", [str(host_ip_match_from_fib)], [str(host_mac), str(sw_port)])
    

    HOSTS = ['10.0.0.1', '10.0.0.2']
    PORT = 2621

    # For kernel learnt routes
    fib_for_forwarding_kernel = []
    # For zebra/OSPF learnt routes
    fib_for_forwarding_zebra = []

    # FInal FIB with all routes
    fib_for_forwarding = []

    # Create socket to bind to FPM server
    def new_socket_create(self):

        num = sys.argv[1]
        HOST = Controller.HOSTS[int(num[1])-1]
        #HOST = "127.0.0.1"
        print(HOST)

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try: 
            s.bind((HOST, Controller.PORT))

        except socket.error as msg:
            print('Bind failed. Error Code : ' + str(msg[0]) + ' Message ' + msg[1])
            sys.exit()
            
        print('Socket bind complete')
        return s

    # Decoder to write fpm message
    def decoder(self, mod, data):

        s = mod.split('.')
        package = '.'.join(s[:-1])
        module = s[-1]
        #print(package, module)
        m = import_module(package)
        met = getattr(m, module)
        #print(met)
        f = open(data, 'r')
        data = load_dump(f) 

        offset = 0
        inbox = []
        msg = met(data[offset:])
        msg.decode()


        router  = sys.argv[1]
        fname = router+"route"+".data"
        
        f = open(fname,"a")
        f.write(str(msg)+"\n")
        f.close()

        attrs = msg['attrs']
        hdr = msg['header']

        if len(attrs) == 3:
            Controller.fib_for_forwarding_kernel.append((attrs[0],attrs[2]))
        else:
            Controller.fib_for_forwarding_zebra.append((attrs[0],attrs[3]))


        
    # Parse fpm message into useful form 
    def read_fpm_message(self,data):

        #Inspired by FRR_p$ project under nsg.ethz.ch

        ''' Start reading the messages after the verion (1 byte) and protocol = {1 : netlink, 2 : protobuf} (1 byte)
        msg length = 2 bytes

        0                   1                   2                   3
        0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1 2 3 4 5 6 7 8 9 0 1
        +---------------+---------------+-------------------------------+
        | Version       | Message type  | Message length                |
        +---------------+---------------+-------------------------------+
        | Data...                                                       |
        +---------------------------------------------------------------+

        unpack every 2 bytes and parse it
        '''

        start = 0
        hdr_length = 4 

        value = 0

        while value < len(data):

            version = data[start]
            protocol = data[start+1]

            if version != 1: 
                print(("incorrect version", version))

            if protocol != 1:
                print(("netlink not found, instead = ", protocol))
            else:
                tot_len = unpack('!H', data[start+2:start+4])[0]
                #print(tot_len)
                value = start + tot_len
                nt_msg = unpack('!' + str(tot_len-hdr_length) +'c', data[start+4:value])
                #print(nt_msg)

                nt_msg = ":".join("{:02x}".format(ord(c)) for c in nt_msg)
                file_name = 'nt_msg'+sys.argv[1]+'.data'
                with open(file_name, 'w') as f:
                    f.write(nt_msg)
                

                t = self.decoder('pyroute2.netlink.rtnl.rtmsg.rtmsg', file_name)
                #print(t)
                start = value
                

    # Fetch the fpm message
    def get_fpm(self, conn, addr):

        with conn:
            print('Connected with ' + addr[0] + ':' + str(addr[1]))
            flag = True

            while flag:
                data = conn.recv(2048)
                self.read_fpm_message(data)

                if not data:
                    print("no data")
                    flag = False

                return None
        
    # Contol block for fpm code
    def main_fpm(self):

        router  = sys.argv[1]
        fname = router+"route"+".data"

        os.system("rm -f {fname}".format(fname = fname))

        s = self.new_socket_create()
        s.listen()
        print("Listening....")

        conn, addr = s.accept()

        self.get_fpm(conn, addr)
        s.close()

        for item in Controller.fib_for_forwarding_kernel:
            Controller.fib_for_forwarding.append(item)

        for item in Controller.fib_for_forwarding_zebra:
            Controller.fib_for_forwarding.append(item)

        return Controller.fib_for_forwarding


if __name__== "__main__":
   
    controller = Controller()