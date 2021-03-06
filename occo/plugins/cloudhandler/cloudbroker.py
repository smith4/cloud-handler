### Copyright 2014, MTA SZTAKI, www.sztaki.hu
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

""" CloudBroker implementation of the
:class:`~occo.cloudhandler.cloudhandler.CloudHandler` class.

.. moduleauthor:: Zoltan Farkas <zoltan.farkas@sztaki.mta.hu>
"""

from __future__ import absolute_import
import urlparse
import occo.util.factory as factory
from occo.util import wet_method, coalesce
from occo.cloudhandler import CloudHandler, Command
import itertools as it
import logging
import occo.constants.status as status
import requests, json, uuid
import xml.dom.minidom
from xml.dom.minidom import parseString
from xml.etree import ElementTree
from xml.etree.ElementTree import Element, SubElement, tostring
from time import sleep
import xml.etree.ElementTree as ET

__all__ = ['CloudBrokerCloudHandler']

PROTOCOL_ID='cloudbroker'

log = logging.getLogger('occo.cloudhandler.cloudbroker')

def get_instance(conn, instance_id):
    reservations = conn.get_all_reservations(instance_ids=[instance_id])
    # TODO: ASSUMING len(reservations)==1 and len(instances)==1
    return reservations[0].instances[0]

def get_auth(auth_data):
    return (auth_data['email'], auth_data['password'])

def get_instance(cloud_handler, jobid):
    attempt = 0
    stime = 1
    while attempt < 3:
        r = requests.get(cloud_handler.target + '/instances.xml',
                auth=get_auth(cloud_handler.auth_data), params={'job_id': jobid})
        if (r.status_code != 200):
            return None
        DOMTree = xml.dom.minidom.parseString(r.text)
        instance = DOMTree.documentElement
        if 0 != instance.getElementsByTagName('instance').length:
            return instance
        sleep(stime)
        stime = stime * 2
    raise Exception()

def getTagText(nodelist):
    rc = []
    for node in nodelist:
        if node.nodeType == node.TEXT_NODE:
            rc.append(node.data)
    return ''.join(rc)

class CreateNode(Command):
    def __init__(self, resolved_node_definition):
        Command.__init__(self)
        self.resolved_node_definition = resolved_node_definition
        self.input_type_id = None

    def _get_input_type_id(self, cloud_handler):
        """Get the ID of the data file type 'input' from CloudBroker."""
        log.debug("[%s] Determining ID of input data type...", cloud_handler.name)
        if self.input_type_id == None:
            dtypes = requests.get(cloud_handler.target + '/data_types.xml',
                auth=get_auth(cloud_handler.auth_data))
            data_types = ET.fromstring(dtypes.text)
            for data_type in data_types.findall('data-type'):
                name = data_type.find('name').text
                dtid = data_type.find('id').text
                if name == 'input':
                    return dtid
            else:
                log.error("[%s] Could not determine data file ID for input files.", cloud_handler.name)
                raise NotImplementedError()

    def _upload_file_with_content(self, cloud_handler, job_id, filename, content):
        """
        Upload a data file for the CloudBroker job by providing the content

        :param str job_id: The identifier of the job in CloudBroker.
        :param str filename: The name of the DataFile.
        :param str content: The content to upload.
        """
        log.debug("[%s] Uploading file %s with content...", cloud_handler.name, filename)
        files = {'data': (filename, content)}
        payload = {'job_id': job_id, 'archive': 'false', 'data_type_id': self.input_type_id}
        req = requests.post(cloud_handler.target + '/data_files.xml',
            auth=get_auth(cloud_handler.auth_data), data=payload, files=files)

    def _upload_file_with_location(self, cloud_handler, job_id, filename, location):
        """
        Upload a data file for the CloudBroker job from a given path

        :param str job_id: The identifier of the job in CloudBroker.
        :param str filename: The name of the DataFile.
        :param str location: The path of the file to upload.
        """
        log.debug("[%s] Uploading file %s with file from path...", cloud_handler.name, filename)
        files = {'data': (filename, open(location, 'rb'))}
        payload = {'job_id': job_id, 'archive': 'false', 'data_type_id': self.input_type_id}
        req = requests.post(cloud_handler.target + '/data_files.xml',
            auth=get_auth(cloud_handler.auth_data), data=payload, files=files)

    def _handle_file(self, cloud_handler, job_id, file_info):
        """
        Handle a file's upload to CloudBroker. The given file may be defined
        either using its content or using the location of the file on the
        filesystem.

        :param str job_id: The identifier of the job
        :param dict file_info: Dictionary discribing the file
        """
        filename = file_info['file_name']
        if 'content' in file_info:
            self._upload_file_with_content(cloud_handler, job_id, filename, file_info['content'])
        elif 'path' in file_info:
            self._upload_file_with_location(cloud_handler, job_id, filename, file_info['path'])

    @wet_method(1)
    def _start_job(self, cloud_handler, software_id, executable_id, resource_id, region_id, instance_type_id, files):
        """
        Start the CloudBroker job.

        :param str software_id: The identifier of the Software in CloudBroker.
        :param str executable_id: The identifier of the Executable in CloudBroker.
        :param str resource_id: The identifier of the Resource in CloudBroker.
        :param str region_id: The identifier of the Region in CloudBroker.
        :param str instance_type_id: The identifier of the instance type in CloudBroker.
        :param list files: List of files to be uploaded for the job

        :Remark: This is a "wet method", the job will not be started
            if the instance is in debug mode (``dry_run``).
        """
        log.debug("[%s] Creating CloudBroker job...", cloud_handler.name)
        self.input_type_id = self._get_input_type_id(cloud_handler)
        jobxml = Element('job')
        name = SubElement(jobxml, 'name')
        name.text = str(uuid.uuid4())
        sid = SubElement(jobxml, 'software-id')
        sid.text = software_id
        eid = SubElement(jobxml, 'executable-id')
        eid.text = executable_id
        resid = SubElement(jobxml, 'resource-id')
        resid.text = resource_id
        regid = SubElement(jobxml, 'region-id')
        regid.text = region_id
        itid = SubElement(jobxml, 'instance-type-id')
        itid.text = instance_type_id
        prtag = SubElement(jobxml, 'permanently-running')
        prtag.text = 'true'
        r = requests.post(cloud_handler.target + '/jobs.xml', tostring(jobxml),
            auth=get_auth(cloud_handler.auth_data),
            headers={'Content-Type': 'application/xml'})
        if (r.status_code == 201):
            DOMTree = xml.dom.minidom.parseString(r.text)
            job = DOMTree.documentElement
            jobid = job.getElementsByTagName('id')[0].childNodes[0].data
            log.debug("[%s] CloudBroker job created: %s, now uploading any input files.", cloud_handler.name, jobid)
            for file_info in files:
                self._handle_file(cloud_handler, jobid, file_info)
            log.debug("[%s] Submitting CloudBroker job...", cloud_handler.name)
            rsubmit = requests.put(cloud_handler.target + '/jobs/' +
                jobid + '/submit.xml', auth=get_auth(cloud_handler.auth_data))
            if (rsubmit.status_code != 200):
                rdelete = requests.delete(cloud_handler.target + '/jobs/' +
                    jobid + '.xml', auth=get_auth(cloud_handler.auth_data))
                jobid = None
            log.debug("[%s] CloudBroker job submitted!", cloud_handler.name)
        else:
            jobid = None
        return jobid

    def perform(self, cloud_handler):
        log.debug("[%s] Creating node: %r",
                cloud_handler.name, self.resolved_node_definition['name'])
        attributes = self.resolved_node_definition['attributes']
        software_id = attributes['software_id']
        executable_id = attributes['executable_id']
        resource_id = attributes['resource_id']
        region_id = attributes['region_id']
        instance_type_id = attributes['instance_type_id']
        files = []
        if 'template_files' in self.resolved_node_definition:
            files += self.resolved_node_definition['template_files']
        if 'files' in self.resolved_node_definition:
            files += self.resolved_node_definition['files']

        job_id = self._start_job(cloud_handler, software_id, executable_id,
            resource_id, region_id, instance_type_id, files)

        log.debug("[%s] Done; job_id = %r", cloud_handler.name, job_id)
        return job_id

class DropNode(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method()
    def _delete_vms(self, cloud_handler, *job_ids):
        """
        Terminate CloudBroker job instances.

        :param job_ids: The list of CloudBroker job identifiers.
        :type job_ids: str

        :Remark: This is a "wet method", termination will not be attempted
            if the instance is in debug mode (``dry_run``).
        """
        for job_id in job_ids:
            r = requests.put(cloud_handler.target + '/jobs/' + job_id + '/stop',
                auth=get_auth(cloud_handler.auth_data))

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

    @wet_method(status.READY)
    def perform(self, cloud_handler):
        r = requests.get(cloud_handler.target + '/jobs/' +
                self.instance_data['instance_id'] + '.xml',
                auth=get_auth(cloud_handler.auth_data))
        if (r.status_code != 200):
            return status.TMP_FAIL
        DOMTree = xml.dom.minidom.parseString(r.text)
        job = DOMTree.documentElement
        retval = job.getElementsByTagName('status')[0].childNodes[0].data
        if retval=="created" or retval=="submitted" or retval=="assembling" or \
                retval=="starting" or retval=="preparing":
                    log.debug("[%s] Done; retval=%r; status=%r",cloud_handler.name,
                            retval, status.PENDING)
                    return status.PENDING
        elif retval=="running":
            log.debug("[%s] Done; retval=%r; status=%r",cloud_handler.name,
                    retval, status.READY)
            return status.READY
        elif retval=="stopping" or retval=="finishing" or retval=="completed":
            log.debug("[%s] Done; retval=%r; status=%r",cloud_handler.name,
                    retval, status.SHUTDOWN)
            return status.SHUTDOWN
        else:
            raise NotImplementedError()

class GetIpAddress(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method('127.0.0.1')
    def perform(self, cloud_handler):
        instance = get_instance(cloud_handler, self.instance_data['instance_id'])
        int_ip = getTagText(instance.getElementsByTagName('internal-ip-address').item(0).childNodes)
        ext_ip = getTagText(instance.getElementsByTagName('external-ip-address').item(0).childNodes)
        log.debug("[%s] Internal IP is: %s, External IP is: %s", cloud_handler.name,
                int_ip, ext_ip)
        return coalesce(ext_ip, int_ip)

class GetAddress(Command):
    def __init__(self, instance_data):
        Command.__init__(self)
        self.instance_data = instance_data

    @wet_method('127.0.0.1')
    def perform(self, cloud_handler):
        instance = get_instance(cloud_handler, self.instance_data['instance_id'])
        int_dns = getTagText(instance.getElementsByTagName('internal-hostname').item(0).childNodes)
        ext_dns = getTagText(instance.getElementsByTagName('external-hostname').item(0).childNodes)
        int_ip = getTagText(instance.getElementsByTagName('internal-ip-address').item(0).childNodes)
        ext_ip = getTagText(instance.getElementsByTagName('external-ip-address').item(0).childNodes)
        log.debug("[%s] Internal IP is: %s, External IP is: %s, Internal hostname is: %s, External hostname is: %s",
                cloud_handler.name, int_ip, ext_ip, int_dns, ext_dns)
        return coalesce(ext_dns, ext_ip, int_dns, int_ip)

@factory.register(CloudHandler, PROTOCOL_ID)
class CloudBrokerCloudHandler(CloudHandler):
    """ Implementation of the
    :class:`~occo.cloudhandler.cloudhandler.CloudHandler` class utilizing the
    CloudBroker_ RESTful_ interface.

    :param str target: Definition of the CloudBroker service URL.
    :param dict auth_data: Authentication infomration for the connection.

        * ``email``: The e-mail address used to log in.
        * ``password``: The password belonging to the e-mail address.

    :param str name: The name of this ``CloudHandler`` instance. If unset,
        ``target['endpoint']`` is used.
    :param bool dry_run: Skip actual resource aquisition, polling, etc.

    .. _CloudBroker: http://cloudbroker.com/
    .. _RESTful: https://en.wikipedia.org/wiki/Representational_state_transfer
    """
    def __init__(self, target, auth_data,
                 name=None, dry_run=False,
                 **config):
        self.dry_run = dry_run
        self.name = name if name else target['endpoint']
        self.target = target if not dry_run else None
        self.auth_data = auth_data if not dry_run else None
        # The following is intentional. It is a constant yet,
        # but maybe it'll change in the future.
        self.resource_type = 'job'

    def cri_create_node(self, resolved_node_definition):
        return CreateNode(resolved_node_definition)

    def cri_drop_node(self, instance_data):
        return DropNode(instance_data)

    def cri_get_state(self, instance_data):
        return GetState(instance_data)

    def cri_get_address(self, instance_data):
        return GetAddress(instance_data)

    def cri_get_ip_address(self, instance_data):
        return GetIpAddress(instance_data)

    def perform(self, instruction):
        instruction.perform(self)
