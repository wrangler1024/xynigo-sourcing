# -*- coding: utf-8 -*-
"""Write Microsoft Excel local images as rich values stored in cells.

``openpyxl`` currently writes pictures as DrawingML objects floating over the
grid.  Excel 365/2024 stores a true "Picture in Cell" as a ``_localImage``
rich value instead.  This module adds that Microsoft OOXML extension to a
newly-created workbook without introducing another runtime dependency.
"""
from io import BytesIO
import re
from xml.etree import ElementTree
import zipfile


CONTENT_TYPES_NS = (
    'http://schemas.openxmlformats.org/package/2006/content-types')
REL_NS = 'http://schemas.openxmlformats.org/package/2006/relationships'
OFFICE_REL_NS = (
    'http://schemas.openxmlformats.org/officeDocument/2006/relationships')
SHEET_NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
RICH_DATA_NS = (
    'http://schemas.microsoft.com/office/spreadsheetml/2017/richdata')
RICH_DATA2_NS = (
    'http://schemas.microsoft.com/office/spreadsheetml/2017/richdata2')
RICH_REL_NS = (
    'http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel')
MC_NS = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
RICH_VALUE_GUID = '{3e2802c4-a4d2-4d8b-9148-e3be6c30e623}'

METADATA_PART = 'xl/metadata.xml'
RICH_REL_PART = 'xl/richData/richValueRel.xml'
RICH_VALUE_PART = 'xl/richData/rdrichvalue.xml'
RICH_STRUCTURE_PART = 'xl/richData/rdrichvaluestructure.xml'
RICH_TYPES_PART = 'xl/richData/rdRichValueTypes.xml'
RICH_REL_RELS_PART = 'xl/richData/_rels/richValueRel.xml.rels'

CELL_REFERENCE_RE = re.compile(r'^[A-Z]{1,3}[1-9][0-9]*$')


def _qname(namespace, name):
    return '{%s}%s' % (namespace, name)


def _xml_bytes(root):
    return ElementTree.tostring(
        root, encoding='utf-8', xml_declaration=True)


def _metadata_xml(count):
    ElementTree.register_namespace('xlrd', RICH_DATA_NS)
    root = ElementTree.Element(_qname(SHEET_NS, 'metadata'))
    metadata_types = ElementTree.SubElement(
        root, _qname(SHEET_NS, 'metadataTypes'), {'count': '1'})
    ElementTree.SubElement(metadata_types, _qname(SHEET_NS, 'metadataType'), {
        'name': 'XLRICHVALUE', 'minSupportedVersion': '120000',
        'copy': '1', 'pasteAll': '1', 'pasteValues': '1', 'merge': '1',
        'splitFirst': '1', 'rowColShift': '1', 'clearFormats': '1',
        'clearComments': '1', 'assign': '1', 'coerce': '1',
    })
    future = ElementTree.SubElement(
        root, _qname(SHEET_NS, 'futureMetadata'),
        {'name': 'XLRICHVALUE', 'count': str(count)})
    for index in range(count):
        block = ElementTree.SubElement(future, _qname(SHEET_NS, 'bk'))
        extensions = ElementTree.SubElement(
            block, _qname(SHEET_NS, 'extLst'))
        extension = ElementTree.SubElement(
            extensions, _qname(SHEET_NS, 'ext'),
            {'uri': RICH_VALUE_GUID})
        ElementTree.SubElement(
            extension, _qname(RICH_DATA_NS, 'rvb'), {'i': str(index)})
    values = ElementTree.SubElement(
        root, _qname(SHEET_NS, 'valueMetadata'), {'count': str(count)})
    for index in range(count):
        block = ElementTree.SubElement(values, _qname(SHEET_NS, 'bk'))
        ElementTree.SubElement(
            block, _qname(SHEET_NS, 'rc'),
            {'t': '1', 'v': str(index)})
    return _xml_bytes(root)


def _rich_value_rel_xml(count):
    ElementTree.register_namespace('r', OFFICE_REL_NS)
    root = ElementTree.Element(_qname(RICH_REL_NS, 'richValueRels'))
    for index in range(1, count + 1):
        ElementTree.SubElement(
            root, _qname(RICH_REL_NS, 'rel'),
            {_qname(OFFICE_REL_NS, 'id'): 'rId%d' % index})
    return _xml_bytes(root)


def _rich_value_data_xml(count):
    root = ElementTree.Element(
        _qname(RICH_DATA_NS, 'rvData'), {'count': str(count)})
    for index in range(count):
        value = ElementTree.SubElement(
            root, _qname(RICH_DATA_NS, 'rv'), {'s': '0'})
        ElementTree.SubElement(
            value, _qname(RICH_DATA_NS, 'v')).text = str(index)
        # CalcOrigin.Standalone: the image is stored directly in the cell.
        ElementTree.SubElement(
            value, _qname(RICH_DATA_NS, 'v')).text = '5'
    return _xml_bytes(root)


def _rich_value_structure_xml():
    root = ElementTree.Element(
        _qname(RICH_DATA_NS, 'rvStructures'), {'count': '1'})
    structure = ElementTree.SubElement(
        root, _qname(RICH_DATA_NS, 's'), {'t': '_localImage'})
    ElementTree.SubElement(
        structure, _qname(RICH_DATA_NS, 'k'),
        {'n': '_rvRel:LocalImageIdentifier', 't': 'i'})
    ElementTree.SubElement(
        structure, _qname(RICH_DATA_NS, 'k'),
        {'n': 'CalcOrigin', 't': 'i'})
    return _xml_bytes(root)


def _rich_value_types_xml():
    ElementTree.register_namespace('mc', MC_NS)
    ElementTree.register_namespace('x', SHEET_NS)
    root = ElementTree.Element(
        _qname(RICH_DATA2_NS, 'rvTypesInfo'),
        {_qname(MC_NS, 'Ignorable'): 'x', 'xmlns:x': SHEET_NS})
    global_node = ElementTree.SubElement(
        root, _qname(RICH_DATA2_NS, 'global'))
    flags = ElementTree.SubElement(
        global_node, _qname(RICH_DATA2_NS, 'keyFlags'))
    definitions = (
        ('_Self', ('ExcludeFromFile', 'ExcludeFromCalcComparison')),
        ('_DisplayString', ('ExcludeFromCalcComparison',)),
        ('_Flags', ('ExcludeFromCalcComparison',)),
        ('_Format', ('ExcludeFromCalcComparison',)),
        ('_SubLabel', ('ExcludeFromCalcComparison',)),
        ('_Attribution', ('ExcludeFromCalcComparison',)),
        ('_Icon', ('ExcludeFromCalcComparison',)),
        ('_Display', ('ExcludeFromCalcComparison',)),
        ('_CanonicalPropertyNames', ('ExcludeFromCalcComparison',)),
        ('_ClassificationId', ('ExcludeFromCalcComparison',)),
    )
    for key_name, flag_names in definitions:
        key = ElementTree.SubElement(
            flags, _qname(RICH_DATA2_NS, 'key'), {'name': key_name})
        for flag_name in flag_names:
            ElementTree.SubElement(
                key, _qname(RICH_DATA2_NS, 'flag'),
                {'name': flag_name, 'value': '1'})
    return _xml_bytes(root)


def _rich_value_relationships(images):
    root = ElementTree.Element(_qname(REL_NS, 'Relationships'))
    for index, _item in enumerate(images, start=1):
        ElementTree.SubElement(root, _qname(REL_NS, 'Relationship'), {
            'Id': 'rId%d' % index,
            'Type': OFFICE_REL_NS + '/image',
            'Target': '../media/image%d.jpeg' % index,
        })
    return _xml_bytes(root)


def _next_relationship_id(root):
    highest = 0
    for item in root.findall(_qname(REL_NS, 'Relationship')):
        match = re.match(r'^rId([0-9]+)$', item.attrib.get('Id', ''))
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _add_workbook_relationships(data):
    root = ElementTree.fromstring(data)
    next_id = _next_relationship_id(root)
    relationships = (
        ('http://schemas.openxmlformats.org/officeDocument/2006/'
         'relationships/sheetMetadata', 'metadata.xml'),
        ('http://schemas.microsoft.com/office/2022/10/'
         'relationships/richValueRel', 'richData/richValueRel.xml'),
        ('http://schemas.microsoft.com/office/2017/06/'
         'relationships/rdRichValue', 'richData/rdrichvalue.xml'),
        ('http://schemas.microsoft.com/office/2017/06/'
         'relationships/rdRichValueStructure',
         'richData/rdrichvaluestructure.xml'),
        ('http://schemas.microsoft.com/office/2017/06/'
         'relationships/rdRichValueTypes', 'richData/rdRichValueTypes.xml'),
    )
    for rel_type, target in relationships:
        ElementTree.SubElement(root, _qname(REL_NS, 'Relationship'), {
            'Id': 'rId%d' % next_id, 'Type': rel_type, 'Target': target,
        })
        next_id += 1
    return _xml_bytes(root)


def _add_content_types(data):
    root = ElementTree.fromstring(data)
    defaults = root.findall(_qname(CONTENT_TYPES_NS, 'Default'))
    if not any(item.attrib.get('Extension', '').lower() == 'jpeg'
               for item in defaults):
        ElementTree.SubElement(root, _qname(CONTENT_TYPES_NS, 'Default'), {
            'Extension': 'jpeg', 'ContentType': 'image/jpeg',
        })
    overrides = (
        ('/xl/metadata.xml',
         'application/vnd.openxmlformats-officedocument.spreadsheetml.'
         'sheetMetadata+xml'),
        ('/xl/richData/richValueRel.xml',
         'application/vnd.ms-excel.richvaluerel+xml'),
        ('/xl/richData/rdrichvalue.xml',
         'application/vnd.ms-excel.rdrichvalue+xml'),
        ('/xl/richData/rdrichvaluestructure.xml',
         'application/vnd.ms-excel.rdrichvaluestructure+xml'),
        ('/xl/richData/rdRichValueTypes.xml',
         'application/vnd.ms-excel.rdrichvaluetypes+xml'),
    )
    existing = {
        item.attrib.get('PartName')
        for item in root.findall(_qname(CONTENT_TYPES_NS, 'Override'))
    }
    for part_name, content_type in overrides:
        if part_name not in existing:
            ElementTree.SubElement(
                root, _qname(CONTENT_TYPES_NS, 'Override'), {
                    'PartName': part_name, 'ContentType': content_type,
                })
    return _xml_bytes(root)


def _add_cell_metadata(data, images):
    root = ElementTree.fromstring(data)
    for index, (cell_reference, _image_data) in enumerate(images, start=1):
        cell = root.find(
            './/%s[@r="%s"]' % (_qname(SHEET_NS, 'c'), cell_reference))
        if cell is None:
            raise ValueError('missing image target cell: %s' % cell_reference)
        cell.attrib['t'] = 'e'
        cell.attrib['vm'] = str(index)
        for child in list(cell):
            cell.remove(child)
        ElementTree.SubElement(
            cell, _qname(SHEET_NS, 'v')).text = '#VALUE!'
    return _xml_bytes(root)


def embed_cell_images(xlsx_bytes, images, sheet_path='xl/worksheets/sheet1.xml'):
    """Return *xlsx_bytes* with JPEG images stored in the target cells.

    ``images`` is an iterable of ``(cell_reference, jpeg_bytes)`` pairs.  The
    workbook must be a freshly generated file without pre-existing rich-data
    parts.  Each image is self-contained under ``xl/media``; no external
    relationship or URL is written.
    """
    images = list(images)
    if not images:
        return xlsx_bytes
    seen_cells = set()
    for cell_reference, image_data in images:
        if not CELL_REFERENCE_RE.match(cell_reference):
            raise ValueError('invalid image target cell')
        if cell_reference in seen_cells:
            raise ValueError('duplicate image target cell: %s' % cell_reference)
        seen_cells.add(cell_reference)
        if (not isinstance(image_data, bytes) or
                not image_data.startswith(b'\xff\xd8') or
                not image_data.endswith(b'\xff\xd9')):
            raise ValueError('invalid JPEG screenshot data')

    source = BytesIO(xlsx_bytes)
    with zipfile.ZipFile(source, 'r') as archive:
        infos = archive.infolist()
        entries = {info.filename: archive.read(info.filename)
                   for info in infos}

    required = {'[Content_Types].xml', 'xl/_rels/workbook.xml.rels',
                sheet_path}
    missing = sorted(required.difference(entries))
    if missing:
        raise ValueError('invalid xlsx package: missing %s' % ', '.join(missing))
    new_parts = {
        METADATA_PART, RICH_REL_PART, RICH_VALUE_PART, RICH_STRUCTURE_PART,
        RICH_TYPES_PART, RICH_REL_RELS_PART,
    }
    new_parts.update(
        'xl/media/image%d.jpeg' % index
        for index in range(1, len(images) + 1))
    conflicts = sorted(new_parts.intersection(entries))
    if conflicts:
        raise ValueError('xlsx already contains rich image parts')

    replacements = {
        '[Content_Types].xml': _add_content_types(
            entries['[Content_Types].xml']),
        'xl/_rels/workbook.xml.rels': _add_workbook_relationships(
            entries['xl/_rels/workbook.xml.rels']),
        sheet_path: _add_cell_metadata(entries[sheet_path], images),
    }
    additions = {
        METADATA_PART: _metadata_xml(len(images)),
        RICH_REL_PART: _rich_value_rel_xml(len(images)),
        RICH_VALUE_PART: _rich_value_data_xml(len(images)),
        RICH_STRUCTURE_PART: _rich_value_structure_xml(),
        RICH_TYPES_PART: _rich_value_types_xml(),
        RICH_REL_RELS_PART: _rich_value_relationships(images),
    }
    for index, (_cell_reference, image_data) in enumerate(images, start=1):
        additions['xl/media/image%d.jpeg' % index] = image_data

    output = BytesIO()
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as archive:
        for info in infos:
            archive.writestr(info, replacements.get(
                info.filename, entries[info.filename]))
        for name, data in additions.items():
            archive.writestr(name, data)
    return output.getvalue()
