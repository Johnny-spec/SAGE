import xml.etree.ElementTree as ET
import os

path = r"C:\Users\wshao\Downloads\AuthCapture_HQ-B16809_281\AuthCapture_HQ-B16809_28145\wfp-filters-udp389.xml"
tree = ET.parse(path)
root = tree.getroot()
filters_elem = root.find("filters")

# 1. 28 BLOCK filters listing
block_filters = []
for f in filters_elem:
    act_type = f.find("action/type")
    if act_type is not None and act_type.text == "FWP_ACTION_BLOCK":
        block_filters.append(f)

print(f"BLOCK FILTERS COUNT: {len(block_filters)}")
for idx, f in enumerate(block_filters, 1):
    fid = f.find("filterId").text
    name = f.find("displayData/name").text if (f.find("displayData/name") is not None and f.find("displayData/name").text is not None) else "None"
    layer = f.find("layerKey").text
    cond_elem = f.find("filterCondition")
    cond_strs = []
    if cond_elem is not None:
        for c in cond_elem.findall("item"):
            fkey = c.find("fieldKey").text
            mtype = c.find("matchType").text
            # Attempt to extract value representation
            val_node = c.find("conditionValue")
            vtype = ""
            vval = "Unknown"
            if val_node is not None:
                vtype_node = val_node.find("type")
                vtype = vtype_node.text if vtype_node is not None else ""
                for child in val_node:
                    if child.tag == "type":
                        continue
                    if child.tag == "byteBlob":
                        as_str = child.find("asString")
                        vval = as_str.text if as_str is not None else "byteBlob"
                    elif child.tag == "v4AddrMask":
                        addr = child.find("addr").text if child.find("addr") is not None else "0.0.0.0"
                        mask = child.find("mask").text if child.find("mask") is not None else "0.0.0.0"
                        vval = f"{addr}/{mask}"
                    elif len(list(child)) > 0:
                        vval = {s.tag: s.text for s in child}
                    else:
                        vval = child.text
            cond_strs.append(f"{fkey} {mtype} {vtype}({vval})")
    cond_join = " AND ".join(cond_strs) if cond_strs else "Empty (Universal)"
    print(f"Block {idx}: ID={fid} | Name={name} | Layer={layer} | Conditions: {cond_join}")

# 2. UniKNet Filters analysis
unik_filters = []
for f in filters_elem:
    name_node = f.find("displayData/name")
    desc_node = f.find("displayData/description")
    name_str = name_node.text if (name_node is not None and name_node.text is not None) else ""
    desc_str = desc_node.text if (desc_node is not None and desc_node.text is not None) else ""
    if "uniknet" in name_str.lower() or "uniknet" in desc_str.lower():
        unik_filters.append(f)

print(f"\nUNIKNET FILTERS COUNT: {len(unik_filters)}")
for idx, f in enumerate(unik_filters, 1):
    fid = f.find("filterId").text
    name = f.find("displayData/name").text if (f.find("displayData/name") is not None and f.find("displayData/name").text is not None) else "None"
    layer = f.find("layerKey").text
    act_type = f.find("action/type").text if f.find("action/type") is not None else "None"
    cond_elem = f.find("filterCondition")
    cond_text = "Empty/Universal"
    if cond_elem is not None and len(cond_elem.findall("item")) > 0:
        cond_text = "Address-specific / Condition-based"
    print(f"UniKNet {idx}: ID={fid} | Name={name} | Layer={layer} | Action={act_type} | Conditions={cond_text}")
