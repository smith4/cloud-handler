### Copyright 2016, MTA SZTAKI, www.sztaki.hu
###
### Licensed under the Apache License, Version 2.0 (the "License");
### you may not use this file except in compliance with the License.
### You may obtain a copy of the License at
###
###    http://www.apache.org/licenses/LICENSE-2.0
###
### Unless required by applicable law or agreed to in writing, software
### distributed under the License is distributed on an "AS IS" BASIS,
### WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
### See the License for the specific language governing permissions and
### limitations under the License.

""" OCCI implementation of the
:class:`~occo.cloudhandler.cloudhandler.CloudHandler` class.

.. moduleauthor:: Zoltan Farkas <zoltan.farkas@sztaki.mta.hu>
"""

import time
import uuid
import urlparse
import occo.util.factory as factory
from occo.util import wet_method, coalesce, basic_run_process
from occo.cloudhandler import CloudHandler, Command
import itertools as it
import logging
import occo.constants.status as status
import subprocess
import json
from pprint import pprint

__all__ = ['OCCICloudHandler']

PROTOCOL_ID = 'occi'
STATE_MAPPING = {
    'waiting'         : status.PENDING,
    'inactive'        : status.PENDING,
    'active'          : status.READY,
    'suspended'       : status.SHUTDOWN,
}

log = logging.getLogger('occo.cloudhandler.nova')

def execute_command(target, auth_data, *args, **kwargs):
    """
    Execute a custom command towards the target.
    """
    cmd = ["occi", "-X", "-n", "x509", "-x", auth_data, "-e", target['endpoint']]
    cmd.extend(args)
    #log.debug("Command is: %r", cmd)
    ret, out, err = basic_run_process(" ".join(cmd), input_data=kwargs.get('stdin'))
    #if 'stdin' in kwargs:
        #p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
        #p.stdin.write(kwargs.get('stdin'))
    #else:
        #p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    #out, err = p.communicate()
    log.debug("Command response is: %r", out)
    log.debug("Command stderr response is: %r", err)
    #p.wait()
    return out

class CreateNode(Command):
    def __init__(self, resolved_node_definition):
        Command.__init__(self)
        self.resolved_node_definition = resolved_node_definition

    @wet_method(1)
    def _start_instance(self, cloud_handler, node_def):
        """
        Start the VM instance.

        :param dict node_def: The resolved node definition to use.

        :Remark: This is a "wet method", the VM will not be started
            if the instance is in debug mode (``dry_run``).
        """
        os_tpl = node_def['os_tpl']
        resource_tpl = node_def['resource_tpl']
        context = node_def['context']
        log.debug("[%s] Creating new server using OS TPL %r and RESOURCE TPL %r",
            cloud_handler.name, os_tpl, resource_tpl)
        server = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a",
            "create", "-r", "compute", "-M", os_tpl, "-M", resource_tpl, "-t",
            "occi.core.title=OCCO_OCCI_VM", "-T", "user_data=file:///dev/stdin",
            stdin=context).splitlines()
        return server[0]

    def perform(self, cloud_handler):
        log.debug("[%s] Creating node: %r",
                  cloud_handler.name, self.resolved_node_definition['name'])

        server = self._start_instance(cloud_handler, self.resolved_node_definition)
        log.debug("[%s] Done; vm_id = %s", cloud_handler.name, server)

        status = 'inactive'
        while status != 'active':
            time.sleep(10)
            description = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a", "describe",
                "-r", server, "-o", "json")
            djson = json.loads(description)[0]
            status = djson['attributes']['occi']['compute']['state']
            log.debug("[%s] Status of VM %s is: %s", cloud_handler.name, server, status)

        if 'link' in self.resolved_node_definition:
            for link in self.resolved_node_definition.get('link'):
                attempts = 0
                while attempts < 10:
                    try:
                        log.debug("[%s] Adding link %s to server...", cloud_handler.name, link)
                        linked = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a", "link", "-r", server,
                            "-j", link)
                    except Exception as e:
                        log.debug(e)
                        time.sleep(1)
                        attempts += 1
                    else:
                        log.debug("[%s] Added link to server", cloud_handler.name)
                        break
                if attempts == 5:
                    log.error("[%s] Failed to add link to server", cloud_handler.name)
                    raise Exception('Failed to add link to server')
        return server

class DropNode(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method()
    def _delete_vms(self, cloud_handler, *vm_ids):
        """
        Terminate VM instances.

        :param vm_ids: The list of VM instance identifiers.
        :type vm_ids: str

        :Remark: This is a "wet method", termination will not be attempted
            if the instance is in debug mode (``dry_run``).
        """
        for server in vm_ids:
            res = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a", "delete", "-r", server)

    def perform(self, cloud_handler):
        """
        Terminate a VM instance.

        :param instance_data: Information necessary to access the VM instance.
        :type instance_data: :ref:`Instance Data <instancedata>`
        """
        instance_id = self.instance_data['instance_id']
        log.debug("[%s] Dropping node %r", cloud_handler.name,
                  self.instance_data['node_id'])

        self._delete_vms(cloud_handler, instance_id)

        log.debug("[%s] Done", cloud_handler.name)

class GetState(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method('ready')
    def perform(self, cloud_handler):
        log.debug("[%s] Acquiring node state %r",
                  cloud_handler.name, self.instance_data['node_id'])
        description = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a", "describe",
            "-r", self.instance_data['instance_id'], "-o", "json")
        djson = json.loads(description)[0]
        inst_state = djson['attributes']['occi']['compute']['state']
        try:
            retval = STATE_MAPPING[inst_state]
        except KeyError:
            raise NotImplementedError('Unknown OCCI state', inst_state)
        else:
            log.debug("[%s] Done; occi_state=%r; status=%r",
                      cloud_handler.name, inst_state, retval)
            return retval

class GetIpAddress(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method('127.0.0.1')
    def perform(self, cloud_handler):
        log.debug("[%s] Acquiring IP address for %r",
                  cloud_handler.name,
                  self.instance_data['node_id'])
        description = execute_command(cloud_handler.target, cloud_handler.auth_data, "-a", "describe",
            "-r", self.instance_data['instance_id'], "-o", "json")
        djson = json.loads(description)
        for link in djson[0]['links']:
            ltype = link['kind']
            lattrs = link['attributes']['occi']
            if 'networkinterface' in lattrs:
                ip = lattrs['networkinterface']['address']
                return ip
        return None

@factory.register(CloudHandler, PROTOCOL_ID)
class OCCICloudHandler(CloudHandler):
    """ Implementation of the
    :class:`~occo.cloudhandler.cloudhandler.CloudHandler` class utilizing the
    OCCI interface.

    :param dict target: Definition of the EC2 endpoint. This must contain:

        * ``endpoint``: URL of the interface.
        * ``regionname``: The name of the EC2 region.

    :param str auth_type: The type of authentication plugin to use.
    :param dict auth_data: Authentication infomration for the connection.

        * ``username``: The access key.
        * ``password``: The secret key.

    :param str name: The name of this ``CloudHandler`` instance. If unset,
        ``target['endpoint']`` is used.
    :param bool dry_run: Skip actual resource aquisition, polling, etc.

    """
    def __init__(self, target, auth_data,
                 name=None, dry_run=False,
                 **config):
        self.dry_run = dry_run
        self.name = name if name else target['auth_url']
        self.target, self.auth_data = target, auth_data
        # The following is intentional. It is a constant yet, but maybe it'll
        # change in the future.
        self.resource_type = 'vm'

    def get_connection(self):
        return setup_connection(self.target, self.auth_data, self.auth_type)

    def cri_create_node(self, resolved_node_definition):
        return CreateNode(resolved_node_definition)

    def cri_drop_node(self, instance_data):
        return DropNode(instance_data)

    def cri_get_state(self, instance_data):
        return GetState(instance_data)

    def cri_get_address(self, instance_data):
        return GetIpAddress(instance_data)

    def cri_get_ip_address(self, instance_data):
        return GetIpAddress(instance_data)

    def perform(self, instruction):
        instruction.perform(self)
