#!/usr/bin/env python3
import fnmatch
import os
import sys
import yaml
import json
import re
import glob
import argparse
import csv
from jinja2 import Template
import yamlordereddictloader
import fs
from shutil import copyfile
from jsonpath_rw import jsonpath, parse
from box import Box
from deepmerge import always_merger


def not_yet_implemented():
    print("Error: NOT YET IMPLEMENTED.")
    exit(1)


# ==========================================================
# Add a patch to the patches structure for the scanned current file
# ==========================================================
def add_patch(patches, current_file, fileRelPath, fileformat, **attributes):
    change = dict()
    for attribute, value in attributes.items():
        change[attribute] = value
    if (current_file is None) or fileRelPath != current_file['file'] or fileformat != current_file['format']:
        current_file = dict()
        current_file['file'] = fileRelPath
        current_file['format'] = fileformat
        current_file['changes'] = list()
        patches.append(current_file)
    current_file['changes'].append(change)
    return current_file


# ==========================================================
# Add an artifact to the artifacts structure. The artifact is unique according the [type, name] key thus avoiding duplicated artifacts 
# The index arg is like a method static var to manage indexes for [type,name] keys
# ==========================================================
def add_artifact(artifacts, type, name, index=dict(), **attributes):
    if type not in artifacts:
        artifacts[type] = list()
        index[type] = dict()
    if name in index[type]:
        artifact = index[type][name]
    else:
        artifact = dict()
        artifact['name'] = name
        artifacts[type].append(artifact)
        index[type][name] = artifact
    for attribute, value in attributes.items():
        artifact[attribute] = value


# ==========================================================
# Resolve with a given context a value contaning possible Jinja rules
# ==========================================================
def resolve(value, context):
    if not type(value) is str:
        return value
    return Template(value).render(context)


# ==========================================================
# Do a global rollback in a folder tree of a patched resource
# All patched files are suffixed by .orig
# ==========================================================
def rollback_resource(args):

    files_to_rollback = []
    target_dir = args.TARGET_PATH
    origin_folder = os.getcwd()
    target_dir = os.path.abspath(target_dir)
    # we work with relative paths to the kubespray root, then we go to this root folder
    os.chdir(target_dir)

    for root, _, filenames in os.walk('.'):
        for filename in fnmatch.filter(filenames, '*.orig'):
            relative_filename = os.path.join(root, filename)
            files_to_rollback.append(relative_filename)

    for filename_orig in files_to_rollback:
        filename = filename_orig[:-5]
        copyfile(filename_orig, filename)
 
    os.chdir(origin_folder)


# ==========================================================
# Create the specific scan resulting of a Kubespray Playbook root folder scan
# Returns the artifacts structured result of the scan
# ==========================================================
def scan_kubespray_playbook(args):
    current_file = None
    artifacts = dict()
    patches = artifacts['patch'] = list()
    yaml_files_to_scan = []
    files_to_scan_regex = []

    for root, _, filenames in os.walk('.'):
        for filename in fnmatch.filter(filenames, 'main.yml'):
            yaml_files_to_scan.append(os.path.join(root, filename))
        for filename in fnmatch.filter(filenames, '*.repo.j2'):
            files_to_scan_regex.append(os.path.join(root, filename))

    pattern_image_repo = re.compile('(.+)_image_repo')
    pattern_download_url = re.compile('(.+)_download_url')
    pattern_image_tag = re.compile('(.+)_image_tag')
    pattern_context_loaded = re.compile('(.+)_version')
    pattern_image_id = re.compile('([^/]+/)?(.*)')
    pattern_url = re.compile(r'(?:https?:\/\/(?:[^\/]+\/)+)([^\/]+)')
    context = {'image_arch': 'amd64'}
    
    # Merge optional additonal context from arguments
    if not args.context is None:
        for key, value in args.context:
            context[key] = value 

    # Scann all yaml files of the current folder
    for filename in yaml_files_to_scan:

        with open(filename, "r", encoding='utf8') as yamlFile:
            doc_data = yaml.load(yamlFile, Loader=yamlordereddictloader.Loader)
            fileformat = 'yaml'
            if doc_data is None:
                continue
            data = doc_data if type(doc_data) is list else [doc_data]

            for elem in data:

                # Because faster and simpler and enouth for now: only global  yaml vars are considered, no recursion into child elements
                for key, value in elem.items():

                    # Case of an empty string, we ignore the key
                    if type(value) is str and len(value) == 0: continue
                        
                    if pattern_context_loaded.match(key) is not None:
                        # Case of a yaml var that could be used by the Jinja templating for searched artifacts var values
                        context[key] = resolve(value, context)
                        continue

                    varname_matching = pattern_image_tag.match(key)
                    if varname_matching is not None:
                        # Case of a yaml var {name}_image_tag
                        name = varname_matching.group(1)
                        context[key] = resolved_value = resolve(
                            value, context)
                        add_artifact(artifacts, type='image_repo', name=name,
                                     tag=resolved_value)

                    varname_matching = pattern_image_repo.match(key)
                    if varname_matching is not None:
                        # Case of a yaml var {name}_image_repo
                        name = varname_matching.group(1)
                        context[key] = resolved_value = resolve(
                            value, context)
                        image_id_matching = pattern_image_id.match(
                            resolved_value)
                        if image_id_matching is not None:
                            # the {name}_image_repo value is an image provided by an Internet URL, so we create a patch changing the registry prefix of this value
                            # to use a local registry where the image will be pushed in
                            current_file = add_patch(patches, current_file, filename, fileformat, json_path = varname_matching.group(0),
                                                     new_value = '{{{{ local_registry }}}}/{}'.format(
                                                         image_id_matching.group(2)),
                                                     old_value = value,
                                                     name = name)
                            # The image downloaded from the Internet mut be pushed to the local registry so it is added to the list of artifacts to dump
                            add_artifact(artifacts, type='image_repo', name=name,
                                         ref=resolved_value, image=image_id_matching.group(2))
                            continue

                    varname_matching = pattern_download_url.match(key)
                    if varname_matching is not None:
                        # Case of a yaml var {name}_download_url
                        name = varname_matching.group(1)
                        context[key] = resolved_value = resolve(
                            value, context)
                        artifact_url = pattern_url.match(resolved_value)
                        if artifact_url is not None:
                            download_url_filename = artifact_url.group(1)
                            # The {name}_image_repo value is a file Internet download, we change the URL to use a local http or file repo
                            current_file = add_patch(patches, current_file, filename, fileformat, json_path = varname_matching.group(0),
                                                     new_value = '{{{{ local_folder }}}}/{}'.format(
                                                         download_url_filename),
                                                     old_value = value,
                                                     name = name)
                            # The file downloaded from the Internet must be pushed to the local repo so it is added to the list of artifacts to dump
                            add_artifact(artifacts, type='download_url', name=name,
                                         url=resolved_value, filename=download_url_filename)

    # Scan not structured files. 
    # And no ulike for Helm Charts, YAML files are not scanned as unstructured files
    # so we don't execute files_to_scan_regex.extend(yaml_files_to_scan)
    
    # scan git clone <url<name>> <folder>
    # don't use this more comple able to capture the [named section] containing a given gpgcheck 
    #gpgcheck_re = re.compile(r'\[(?P<section_name>[^\n]*)\](?:(?!\n\[[^\n].*|\ngpgcheck=1)\n[^\n]*)*\n(?P<left_term>gpgcheck\s*=)\s*(?P<value>1)', re.MULTILINE + re.DOTALL)
    gpgcheck_re = re.compile(r'^(?P<left_term>gpgcheck\s*=)\s*(?P<value>1)$', re.MULTILINE)

    for filename in files_to_scan_regex:
        with open(filename, "r", encoding='utf8') as unstructured_file:
            data = unstructured_file.read()
            for item in re.finditer(gpgcheck_re, data):
                left_term, value = item.group('left_term'), item.group('value')
                new_value = '{left_term}{value}'.format(left_term=left_term, value='0')
                current_file = add_patch(patches, current_file, filename, 'text', new_value=new_value, old_value=item.group(0))

    return artifacts


# ==========================================================
# Do a recursive scan of an OpenWhisk Helm Chart part elemTuple at given jsonpath for a current scanned file
# Add scanned dependencies resources in artifacts and change to be done in patches structure
# ==========================================================
def scan_helm_chart_element(artifacts, patches, current_file, filename, fileformat, jsonPath, elemTuple):

    elem, keyElem, parentTuple = elemTuple
    if type(elem) is list:
        index = 0
        for item in elem:
            indexPath = '[{}]'.format(index)
            current_file = scan_helm_chart_element(artifacts, patches, current_file, filename, fileformat, indexPath if jsonPath is None else jsonPath + indexPath, (item, index, elemTuple))
            index += 1
    elif type(elem) is dict or isinstance(elem, yamlordereddictloader.OrderedDict):
        pattern_image_id = re.compile('([^/]+/)?(.*)')
        # Because faster and simpler and enouth for now: only global  yaml vars are considered, no recursion into child elements
        for key, value in elem.items():
            if (key == 'imageName' or key == 'image') and type(value) is str:
                # Case of a yaml var imageName
                #name = elem['name'] if 'name' in elem else jsonPath
                image_id_matching = pattern_image_id.match(value)
                if image_id_matching is not None:
                    # the value is an image provided by an Internet docker image name, so we create a patch changing the registry prefix of this value
                    # to use a local registry where the image will be pushed in                
                    imageRef = value
                    imageName = image_id_matching.group(2)
                    imageTag = elem['imageTag'] if 'imageTag' in elem else None
                    imageNameTag = imageName if imageTag is None else imageName + ':' + imageTag
                    current_file = add_patch(patches, current_file, filename, fileformat, json_path = key if jsonPath is None else jsonPath + '.' + key,
                                                new_value = '{{{{ local_registry }}}}/{}'.format(imageName),
                                                old_value = value,
                                                name = imageName)
                    # The image downloaded from the Internet mut be pushed to the local registry so it is added to the list of artifacts to dump
                    add_artifact(artifacts, type='image_repo', name=imageNameTag,
                                    ref=imageRef, image=imageRef, tag=imageTag)
            elif key == 'name' and type(value) is str and ('prefix' in elem or 'tag' in elem) and (keyElem == 'image' or (parentTuple[1] == 'blackboxes')):
                # Case of a json var of type docker image
                imageName = value
                imagePrefix = elem['prefix'] if 'prefix' in elem else None
                imageRef = imageName if imagePrefix is None else imagePrefix + '/' + imageName
                imageTag = elem['tag'] if 'tag' in elem else None
                imageNameTag = imageName if imageTag is None else imageName + ':' + imageTag
                # the value is an image provided by an Internet docker image name, so we create a patch changing the registry prefix of this value
                # to use a local registry where the image will be pushed in                
                current_file = add_patch(patches, current_file, filename, fileformat, json_path = jsonPath + '.prefix',
                                            new_value = '{{ local_registry }}',
                                            old_value = imagePrefix,
                                            name = imageName)
                # The image downloaded from the Internet mut be pushed to the local registry so it is added to the list of artifacts to dump
                add_artifact(artifacts, type='image_repo', name=imageNameTag,
                                ref=imageRef, image=imageRef, tag=imageTag)
            else:
                current_file = scan_helm_chart_element(artifacts, patches, current_file, filename, fileformat, key if jsonPath is None else jsonPath + '.' + key, (value, key, elemTuple))


    return current_file


# ==========================================================
# log functions  
# ==========================================================
def log_error(message, args = None):
    log(False, 'Error', message)


def log_alert(message, args = None):
    log(False if args is None else args.quiet, 'Warning', message)


def log(is_quiet, level, message):
    if not is_quiet:
        print('{}:{}'.format(level, message))


# ==========================================================
# evaluate the x value as a python sentence using the context v  
# ==========================================================
def eval_elem(x, v):
    try:
        if type(x) is str:
            return eval(x)
        else:
            return x
    except Exception as e:
        log_error('"{}": {}'.format(x, e))

# ==========================================================
# map a function recursively to the something yaml doc 
# ==========================================================
def recursive_map(something, func):
  if isinstance(something, dict):
    accumulator = {}
    for key, value in something.items():
      accumulator[key] = recursive_map(value, func)
    return accumulator
  elif isinstance(something, (list, tuple, set)):
    accumulator = []
    for item in something:
      accumulator.append(recursive_map(item, func))
    return type(something)(accumulator)
  else:
    return func(something)


# ==========================================================
# insert / update the scan_result yaml doc with the value using the parser as the path to to the value
# ==========================================================
def update_recursively(scan_result, parser, value):
    # Not resolve means the parent (parser.left) must be extended by the parser.right
    tp = type(parser)
    if tp is jsonpath.Child:
        tpr = type(parser.right)
        recurs_value = None
        if tpr is jsonpath.Fields:
            recurs_value = dict()
        elif tpr is jsonpath.Slice:
            recurs_value = []
        else:
            raise Exception('Cannot manage a parser type {} in the recursive update'.format(tpr))
        elem = update_recursively(scan_result, parser.left, recurs_value)
    elif tp is jsonpath.Root:
        return scan_result
    # elem is extended by parser.right
    if tpr is jsonpath.Fields:
        if (len(parser.right.fields) != 1):
            raise Exception('Several paths due to {}'.format(parser.right.fields))
        if not type(elem) is dict:
            raise Exception('Cannot create for the type {} the attribute {}'.format(type(elem), parser.right.fields[0]))
        if not parser.right.fields[0] in elem:
            elem[parser.right.fields[0]] = value
        else:
            elem[parser.right.fields[0]] = always_merger.merge(elem[parser.right.fields[0]], value)
        return elem[parser.right.fields[0]]
    elif tpr is jsonpath.Slice:
        if parser.right.start is None and parser.right.end is None and parser.right.step is None:
            elem.append(value)
            return elem[-1]
    raise Exception('Inconsistent algorithm')


# ==========================================================
# update the scan_result yaml doc using the set yaml descriptor and the context v
# ==========================================================
def update(scan_result, set, v):
    parser = parse(set['path'])
    update_recursively(scan_result, parser, recursive_map(set['value'], lambda x: eval_elem(x, v)))

# ==========================================================
# Create the specific scan resulting of a OpenWhisk Helm Chart root folder scan
# Returns the artifacts structured result of the scan
# ==========================================================
def scan_file(file_system, fs_path, data, actions, args, scan_result):
    # Scan JSON well formed files
    for action in actions:
        if 'get' in action:
            results = [match.value for match in parse(action['get']).find(data)]
        else:
            results = [data]
        for item_data in results:
            v = Box(item_data)
            if 'perform' in action and not any([isinstance(item_data, list), isinstance(item_data, tuple)]):
                perform_list = [action['perform']] if type(action['perform']) is dict else action['perform']
                for perform in perform_list:
                    performed = v['performed'] = []
                    perform_result = None
                    try:
                        if 'finditer' in perform:
                            match_re = re.compile(perform['finditer'], re.MULTILINE)
                            matching_data = recursive_map(perform['value'], lambda x: eval_elem(x, v))
                            perform_result = []
                            for r in re.finditer(match_re, matching_data): perform_result.append(r)
                        elif 'match' in perform:
                            match_re = re.compile(perform['match'], re.MULTILINE)
                            matching_data = recursive_map(perform['value'], lambda x: eval_elem(x, v))
                            perform_result = re.match(match_re, matching_data)
                        else:
                            log_error('{} is an invalid perform item'.format(perform), args)
                        performed.append(perform_result)
                        if 'name' in perform: v[perform['name']] = perform_result
                    except Exception as e:
                        log_error('"{}" {}'.format(perform, e), args)
            # do the set on each get item 
            if 'set' in action:
                # get the hasattr condition
                attr_list = []
                if 'hasattr' in action:
                    attr_list = action['hasattr'] if type(action['hasattr']) is list else [action['hasattr']]
                # get the if condition
                if_condition = 'True'
                if 'if' in action: if_condition = action['if']
                try:
                    for attr in attr_list:
                        if attr[0] == '!':
                            if hasattr(v, attr[1:]):
                                if_condition = 'False'
                                break
                        else:
                            if not hasattr(v, attr):
                                if_condition = 'False'
                                break
                except Exception as e:
                    v = item_data
                try:
                    if eval(if_condition):
                        try:
                            update(scan_result, action['set'], v)
                        except Exception as e:
                            log_error('"{}" {}'.format(action['set'], e), args)
                except Exception as e:
                    log_alert('"{}" {}'.format(if_condition, e), args)

# ==========================================================
# Create the scan resulting of a directory scan
# Returns the artifacts structured result of the scan
# ==========================================================
def scan_resource(args):

    # read configurations
    configs = list()
    with open(args.CONFIG, "r", encoding='utf8') as config_yaml_file:
        config_docs = yaml.load_all(config_yaml_file)
        for config in config_docs:
            configs.append(config)

    # take into account context option
    if type(args.context) is str:
        try:
            # A context for immediate substitution during the apply is provided. We tranform it in JSON/YAML
            args.context = json.loads(args.context)
        except ValueError:
            args.context = eval(args.context)

    ## Take into account rollback option
    if args.rollback: rollback_resource(args)

    target_dir = args.TARGET_PATH
    #(deprecated) origin_folder = os.getcwd()
    target_dir = os.path.abspath(target_dir)
    # we work with relative paths, then we go to this folder
    # (deprecated) os.chdir(target_dir)
    scan_result = dict()
    res = scan_result['resource'] = dict()
    res['path'] = target_dir

    # check the type of resource
    # if os.path.isdir('./roles/kubespray-defaults'):
    #     artifacts = scan_kubespray_playbook(args)
    # elif os.path.isfile('./Chart.yml') or os.path.isfile('./Chart.yaml'):
    #     artifacts = scan_helm_chart(args)
    # else:
    #     print("Error: '{}' seems to be not a kubespray playbook or a helm chart folder".format(target_dir))
    #     exit(1)
    supported_file_contents = ['yaml', 'json', 'text']

    for config in configs:
        for content_type in supported_file_contents:
            for typed_file_patterns in parse('$.{}.[*]'.format(content_type)).find(config):
                print(typed_file_patterns.value)
                inclusions = [match.value for match in parse('[*].include[*]').find(typed_file_patterns.value)]
                exclusions = [match.value for match in parse('[*].exclude[*]').find(typed_file_patterns.value)]
                with fs.open_fs(target_dir) as file_system:
                    for fs_path in file_system.walk.files(filter=None):
                        is_included = True  
                        if len(inclusions) > 0:
                            is_included = False
                            for inclusion in inclusions:
                                if fs.glob.match(inclusion, fs_path):
                                    is_included = True
                                    break
                        for exclusion in exclusions:
                            if fs.glob.match(exclusion, fs_path):
                                is_included = False
                                break
                        if not is_included: continue
                        # The fs_path must be analysed, we get this content
                        try:
                            with file_system.open(fs_path, "r", encoding='utf8') as file_to_scan:
                                if content_type == 'yaml':
                                        data = yaml.load(file_to_scan, Loader=yamlordereddictloader.Loader)
                                elif content_type == 'json':
                                    data = json.load(file_to_scan)
                                elif content_type == 'text':
                                    data = file_to_scan.load()
                                else:
                                    log_alert('{} is a unknown file content type and then ignored. Only {} are supported.'.format(content_type, supported_file_contents), args)
                                # The file content data is scanned
                                if 'actions' in typed_file_patterns.value:
                                    scan_file(file_system, fs_path, data, typed_file_patterns.value['actions'], args, scan_result)
                                else:
                                    log_alert('{} is a unknown file content type and then ignored. Only {} are supported.'.format(content_type, supported_file_contents), args)
                        except yaml.parser.ParserError as ex:
                            log_alert('YAML error {} in {}. The file is ignored.'.format(ex, fs_path), args)
                        except yaml.scanner.ScannerError as ex:
                            log_alert('YAML error {} in {}. The file is ignored.'.format(ex, fs_path), args)
                            


    # restore the current working folder
    #(deprecated) os.chdir(origin_folder)

    # returns the scan result
    return scan_result


# ==========================================================
# Get the output stream from args
# ==========================================================
def get_output_stream(args):
    return sys.stdout if args.output is None else open(args.output, 'w', encoding='utf8')


# ==========================================================
# Get the input stream from args
# ==========================================================
def get_input_stream(args):
    return sys.stdin if args.input is None else open(args.input, 'r', encoding='utf8')


# ==========================================================
# Dump the json structured content to the given stream in a YAML format
# ==========================================================
def dump_as_yaml(content, stream, args):
    yaml.safe_dump(content, stream, default_flow_style=False)
    # (deprecated because introduce 'item': !!python/unicode "some string"
    # see https://stackoverflow.com/questions/20352794/pyyaml-is-producing-undesired-python-unicode-output/20369984
    # yaml.dump(yaml_doc['patches'], sys.stdout, encoding='utf-8', allow_unicode=True,
    #           Dumper=yamlordereddictloader.Dumper)


# ==========================================================
# Dump the json structured content to the given stream in a CSV format
# The columns are defined by args.type
# ==========================================================
def dump_as_csv(content, stream, args, fieldnames_by_type={'download_url': ['name', 'url', 'filename'],
                                                           'image_repo': ['name', 'ref', 'tag', 'image'],
                                                           'git_repo': ['name', 'url', 'folder'],
                                                           'patch': ['file', 'json_path', 'name', 'new_value', 'old_value']}):
    # Convert a patch list to be serializable in CSV
    if args.type == 'patch':
        content_csv = list()
        for patched_file in content:
            for patch in patched_file['changes']:
                patch['file'] = patched_file['file']
                content_csv.append(patch)
        content = content_csv

    listWriter = csv.DictWriter(
        stream,
        fieldnames=fieldnames_by_type[args.type] if args.type in fieldnames_by_type else content[0].keys(
        ),
        delimiter=',',
        quotechar='"',
        quoting=csv.QUOTE_NONNUMERIC
    )
    listWriter.writerows(content)


# ==========================================================
# Dump the json structured content to the given stream in a format specified by args.format  
# ==========================================================
def dump(content, stream, args):
    globals()['dump_as_' + args.format](content, stream, args)


# ==========================================================
# Transform a jsonpath into a list of items like this:
# aaa.bbb[0][5].ccc ==> ('aaa', 'bbb', 0, 5, 'ccc')
# The resulting list facilitates the jsonpath processing
# ==========================================================
def jsonpath_to_list(str_path):
    doted_list = str_path.split('.')
    result = list()
    for path_elem in doted_list:
        elem_array = jsonpath_to_list.pathElem.match(path_elem)
        if elem_array is None:
            raise ValueError('{} is not a valid JSON/YAML path'.format(str_path))
        else:
            if not elem_array.group(1) is None: result.append(elem_array.group(1))
            if not elem_array.group(2) is None: result.append(int(elem_array.group(2)))
    return result

jsonpath_to_list.pathElem = re.compile(r'([^\[]*)(?:\[([0-9]+)\])?')


# ==========================================================
# Function returning a tuple which matches the json_path in thr root strctured object
# Returns a tuple (parent, key to the leaf , leaf) 
# ==========================================================
def get_from_json_path(root, json_path):
    if type(json_path) is str:
        json_path = jsonpath_to_list(json_path)
    key = json_path[0]
    if (isinstance(key, int) and isinstance(root, list) and key < len(root)) or (key in root):
        return (root, key, root[key]) if len(json_path) == 1 else get_from_json_path(root[key], json_path[1:])
    return (None, None, None)


# ==========================================================
# Apply change to a data as a structured content (json or YAML) 
# The change is as it is contained in a scan result 
# Returns True if a change on the data has been done
# ==========================================================
def apply_change(data, change, args, filename):
    has_changed = False
    # Make imediately the template jinja2 exec using a context if it is provided, only on new values
    new_value = change['new_value'] if args.context is None else resolve(change['new_value'], args.context)
    old_value = change['old_value']
    if 'json_path' in change:
        # Case of a JSON / YAML change
        if type(data) is str:
            raise RuntimeError('The file cannot be read in YAML while a YAML modification must be applied to it')
        key = json_path = change['json_path']
        path_end, key, replaced_value = get_from_json_path(data, json_path)
        if not replaced_value is None:
            if replaced_value != old_value and replaced_value != new_value:
                raise RuntimeError("'{}:{}' = '{}' but must be equal to '{}' to be changed to '{}'".format(
                    filename, json_path, replaced_value, old_value, new_value))
            if replaced_value == new_value:
                print("{}: '{}' = '{}' not found because probably the change has been done by a previous change".format(
                        filename, json_path, replaced_value))
            else:
                path_end[key] = new_value
                has_changed = True
                if args.dry_run:
                    print("Dry-run: file {}, '{}' at {} is replaced by '{}'".format(filename, old_value, json_path, new_value))
        else:
            raise RuntimeError('{} not found', json_path)
    else:
        # Case of a textual search and replace
        count = 0
        while data.find(old_value) != -1:
            data = data.replace(old_value, new_value, 1)
            count += 1
            has_changed = True         
        if args.dry_run and count > 0:
            print("Dry-run: file {}, {} '{}' has been replaced by {}".format(filename, str(count), old_value, new_value))
        if count == 0:
            print("{}: '{}' not found because probably the change has been done by a previous change".format(
                    filename, old_value))

    return data, has_changed


# ==========================================================
# Apply changes to a given file of a given format.
# Returns True if the file has been changed
# ==========================================================
def apply_changes_to_file(filename, fileformat, changes, args):
    is_file_changed = False
    is_content_changed = False
    doc_data = None
    with open(filename, "r", encoding='utf8') as file_to_change:
        try:
            # PyYAML/yaml is able to read json too 
            doc_data = yaml.load(file_to_change, Loader=yamlordereddictloader.Loader)
        except:
            # If the file cannot be read as structured format, we work with text related changes
            file_to_change.seek(0)
            doc_data = file_to_change.read()
        for change in changes:
            try:
                doc_data, has_changed = apply_change(doc_data, change, args, filename)
                if has_changed: is_content_changed = True  
            except RuntimeError as err:
                print('Error in file {}: {}'.format(filename, str(err)))
                if not args.dry_run:
                    # We stop immediately
                    raise err
    if is_content_changed:
        if args.rollbackable:
            # The file has been changed, whe make a copy if not already done and then the file is updated
            filename_orig = filename + '.orig'
            if args.dry_run:
                print(
                    "Dry-run: {} has been copied to {}".format(filename, filename_orig))
            else:
                if not os.path.exists(filename_orig):
                    copyfile(filename, filename_orig)
                else:
                    if args.dry_run:
                        print("Dry-run: {} already exists".format(filename_orig))
        if not args.dry_run:
            # Update the file
            with open(filename, 'w', encoding='utf8') as updated_file:
                if type(doc_data) is str and fileformat == 'text':
                    updated_file.write(doc_data)
                else:
                    if fileformat == 'yaml':
                        yaml.dump(doc_data, updated_file, encoding='utf-8', default_flow_style=False,
                                Dumper=yamlordereddictloader.Dumper)
                    elif fileformat == 'json':
                        json.dump(doc_data, updated_file, sort_keys = False, indent = 4, ensure_ascii = False)
                    else:
                        raise RuntimeError('{} is not a supported file format. The file {} has not been changed'.format(fileformat, filename))
        is_file_changed = True

    return is_file_changed


# ==========================================================
# Apply a patch as it is structured in a scan result
# args give options and the target folder where to apply changes
# ==========================================================
def apply_patch(patch, args):
    if args.rollback: rollback_resource(args)

    if type(args.context) is str:
        try:
            # A context for immediate substitution during the apply is provided. We tranform it in JSON/YAML
            args.context = json.loads(args.context)
        except ValueError:
            args.context = eval(args.context)

    target_dir = args.TARGET_PATH
    origin_folder = os.getcwd()
    target_dir = os.path.abspath(target_dir)
    # we work with relative paths to the kubespray root, then we go to this root folder
    os.chdir(target_dir)

    # iterate changes file by file
    for file_changes in patch:
        filename = file_changes['file']
        fileformat = file_changes['format']
        if apply_changes_to_file(filename, fileformat, file_changes['changes'], args):
            if args.dry_run:
                print("Dry-run: {} has been patched".format(filename))
            else:
                print("{} has been patched".format(filename))

    # restore the current folder
    os.chdir(origin_folder)


# ==========================================================
# scan command execution: build a YAML file containing all changes and Internet dependencies required by
#  a kubespray playbook or an Helm Chart to be usd in a offline environment
# ==========================================================
def do_scan(args):
    args.format = 'yaml'
    yaml_doc = scan_resource(args)
    dump(yaml_doc, get_output_stream(args), args)


# ==========================================================
# dump command execution: use as input the YAML file generated by scan.
# Produce the output for a given type of object in the CSV format by default
# ==========================================================
def do_dump(args):
    yaml_doc = yaml.load(get_input_stream(
        args), Loader=yamlordereddictloader.Loader)
    if type(yaml_doc) is dict or isinstance(yaml_doc, yamlordereddictloader.OrderedDict):
        # the yaml_doc seems to be a YAML document, we use it to execute a dump
        dump(yaml_doc[args.type] if args.type in yaml_doc else list(), get_output_stream(args), args)
    else:
        print("Error: the input stream is not a valid YAML for the dump command.")
        exit(1)


# ==========================================================
# apply command execution: use as input the YAML file generated by scan.
# Do changes (or simulate it if --dry-run) in a given folder tree of a
#  Kubespray Ansible Playbook or a OpenWhisk Helm Chart
# ==========================================================
def do_apply(args):
    not_yet_implemented()
    if args.dry_run and args.rollback:
        print("Error: the dry-run and rollback options cannot be used together.")
        exit(1)
    yaml_doc = yaml.load(get_input_stream(
        args), Loader=yamlordereddictloader.Loader)
    if type(yaml_doc) is dict or isinstance(yaml_doc, yamlordereddictloader.OrderedDict):
        apply_patch(yaml_doc['patch'], args)
    else:
        print("Error: the input stream is not a valid YAML for the dump command.")
        exit(1)


# ==========================================================
# roolback command execution
# ==========================================================
def do_rollback(args):
    rollback_resource(args)


# ==========================================================
# list-vars command execution
# ==========================================================
def do_list_vars(args):
    vars_in_new_values = set()
    pattern_jinja_var = re.compile(r'\{\{[\s\r\n]*(?P<var_name>[A-Za-z_][\-A-Za-z_0-9]*)[\s\r\n]*\}\}')
    yaml_doc = yaml.load(get_input_stream(
        args), Loader=yamlordereddictloader.Loader)
    if type(yaml_doc) is dict or isinstance(yaml_doc, yamlordereddictloader.OrderedDict):
        for file_change in yaml_doc['patch']:
            for change in file_change['changes']:
                for var in re.findall(pattern_jinja_var, change['new_value']):
                    vars_in_new_values.add(var)
    else:
        print("Error: the input stream is not a valid YAML for the dump command.")
        exit(1)
    print(vars_in_new_values)

# ==========================================================
# Get the args parser related to given args 
# ==========================================================
def parse_args(args):
    # ==========================================================
    # CLI syntax configuration and documentation defined and processed by the argparse python standard lib
    # ==========================================================
    parser = argparse.ArgumentParser(
        description='CLI tool helper to adapt software configuration by scanning and changing files in a directory', formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--version', action='version', version='1.0.0')
    parser.add_argument('CONFIG', help='configuration file')
    subparsers = parser.add_subparsers()

    scan_parser = subparsers.add_parser(
        'scan', help='make a scan (read only) and produce a YAML result which lists identified elements and patches to apply')
    scan_parser.set_defaults(func=do_scan)

    rollback_parser = subparsers.add_parser(
        'rollback', help='do a rollback using a rollback file generated by a patch command')
    rollback_parser.set_defaults(func=do_rollback)

    dump_parser = subparsers.add_parser(
        'dump', help='dump the input generated by the scan to produce a given type of object in the CSV format by default', formatter_class=argparse.RawTextHelpFormatter)
    dump_parser.set_defaults(func=do_dump)

    apply_parser = subparsers.add_parser(
        'apply', help='apply changes identified by a scan and produce a rollback file if not already exists')
    apply_parser.set_defaults(func=do_apply)

    list_vars_parser = subparsers.add_parser(
        'list-vars', help='list variables you can subsitute on applied changes using the command apply and the option --context')
    list_vars_parser.set_defaults(func=do_list_vars)
    list_vars_parser.add_argument(
        '-i', '--input', help='file path of the source file as the YAML result of the scan command')

    rollback_parser.add_argument('TARGET_PATH',
                            help='Kubespray playbook or helm chart folder location where to apply patches', default='.')
    scan_parser.add_argument('--rollback', action='store_true',
                            help='do a global rollback before')
    scan_parser.add_argument('--quiet', action='store_true',
                            help='do not display warnings and info messages')
    scan_parser.add_argument(
        '-o', '--output', help='file path of the destination file for the YAML result')
    scan_parser.add_argument('TARGET_PATH',
                            help='Kubespray playbook or helm chart folder location', default='.')
    scan_parser.add_argument(
        '--context', help="""context as a json string to resolve scanned values on the fly (example: --context '{"a":"v1","b":3}')""")
    dump_parser.add_argument(
        '-t', '--type', nargs='?', default='patch', choices=['patch', 'download_url', 'image_repo', 'git_repo'], help='type of entry to dump')
    dump_parser.add_argument(
        '-f', '--format', nargs='?', default='csv', choices=['yaml', 'csv'], help='''output format of the dumped data.
    For the CSV format, columns order depends of the type:
        download_url: name, url, filename
        image_repo: name, ref, tag, image
        git_repo: name, url, folder
        patch: file, json_path, name, new_value, old_value''')
    dump_parser.add_argument(
        '-i', '--input', help='file path of the source file as the YAML result of the scan command')
    dump_parser.add_argument(
        '-o', '--output', help='file path of the destination file for the YAML result')
    apply_parser.add_argument(
        '-i', '--input', help='file path of the patch file as the YAML result of the scan command')
    apply_parser.add_argument(
        '--rollbackable', action='store_true', help='files are copied to a same_filename.orig before any change. This copy is used by the --rollback operation')
    apply_parser.add_argument(
        '--context', help="""context as a json string to resolve patched values on the fly (example: --context '{"a":"v1","b":3}')""")
    apply_parser.add_argument('--dry-run', action='store_true',
                            help='do all things whithout changing the playbook or helm chart. All changes are displayed on the standard output')
    apply_parser.add_argument('--rollback', action='store_true',
                            help='do a global rollback before')
    apply_parser.add_argument('TARGET_PATH',
                            help='Kubespray playbook or helm chart folder location where to apply patches', default='.')
    return parser.parse_args(args)

# ==========================================================
# ========================== main ==========================
# ==========================================================

if __name__ == '__main__':
    # ==========================================================
    # Unit testing section, Sorry to not be capable to use advanced unit testing tools, but I'am a kind of beginner in the Python world
    # ==========================================================
    # For local test, uncomment the session you want to test:
    """
    # Full Kubespray test session: --> rollback --> scan --> apply
    sys_argv = sys.argv.copy()
    sys.argv.extend(['rollback', r'c:\projects\src\kubespray'])
    args = parser.parse_args()
    args.func(args)
    sys.argv = sys_argv.copy()
    sys.argv.extend(['scan', '-o', r'c:\projects\src\scan.yml', r'c:\projects\src\kubespray'])
    args = parser.parse_args()
    args.func(args)
    sys.argv = sys_argv.copy()
    #sys.argv.extend(['apply', '--context', '{"local_registry":"W.X.Y.Z:5000","local_hfs":"http://W.X.Y.Z:80"}', '--input', r'c:\projects\src\scan.yml',
    #                 r'c:\projects\src\kubespray'])
    sys.argv.extend(['apply', '--input', r'c:\projects\src\scan.yml', r'c:\projects\src\kubespray'])
    """
    # Helm Chart
    sys.argv.extend(['openwhisk.adapt.yaml', 'scan', '--quiet', '-o', r'c:\projects\src\openwhisk.scan.yml', r'c:\projects\src\incubator-openwhisk-deploy-kube-master.old/helm/openwhisk'])
    #sys.argv.extend(['dump', '--format', 'csv', '--type', 'image_repo', '--input', 'c:/projects/src/chart-scan.yml'])
    #sys.argv.extend(['dump', '--format', 'csv', '--type', 'git_repo', '--input', 'c:/projects/src/chart-scan.yml'])
    #sys.argv.extend(['apply', '--context', "{'local_registry':'W.X.Y.Z:5000'}", '--input', 'c:/projects/src/chart-scan.yml',
    #                 '--dry-run', r'c:\projects\src\incubator-openwhisk-deploy-kube-master.old\helm\openwhisk'])
    """
    # Full Helm test session: --> rollback --> scan --> apply
    sys_argv = sys.argv.copy()
    sys.argv.extend(['rollback', r'C:\projects\src\incubator-openwhisk-deploy-kube-master.old\helm\openwhisk'])
    args = parser.parse_args()
    args.func(args)
    sys.argv = sys_argv.copy()
    sys.argv.extend(['scan', '-o', r'c:\projects\src\chart-scan.yml', r'c:\projects\src\incubator-openwhisk-deploy-kube-master.old/helm/openwhisk'])
    args = parser.parse_args()
    args.func(args)
    sys.argv = sys_argv.copy()
    sys.argv.extend(['apply', '--context', '{"local_registry":"W.X.Y.Z:5000","local_hfs":"http://W.X.Y.Z:80"}', '--input', 'c:/projects/src/chart-scan.yml',
                     r'c:\projects\src\incubator-openwhisk-deploy-kube-master.old\helm\openwhisk'])
    """
    #sys.argv.extend(['rollback', '--help'])
    #sys.argv.extend(['list-vars', '--input', 'c:/projects/src/chart-scan.yml'])
    #sys.argv.extend(['dump', '--help'])

    # ==========================================================
    # Parse args and execute the command with options if the syntax is OK
    # ==========================================================
    parser = parse_args(sys.argv[1:])
    parser.func(parser)
