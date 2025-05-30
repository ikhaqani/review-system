
from flask import (
    render_template, flash, redirect, url_for, Blueprint,
    request, abort, current_app, Response, jsonify
)
from app import db # Ervan uitgaande dat db correct is geïnitialiseerd in app/__init__.py
from app.models import Comment # Zorg ervoor dat dit model correct is gedefinieerd
import json
import os
from datetime import datetime
import pytz # pytz is nodig voor tijdzones
import io
import csv
import re
from collections import defaultdict

bp = Blueprint('main', __name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
# Pad naar je OpenEHR web template JSON-bestand
TEMPLATE_FILEPATH = os.path.join(BASE_DIR, 'templates_openehr', 'ACP-DUTCH.json') 

# Definities van sectie- en containertypes (behoud van bestaande logica)
ORIGINAL_SECTION_TYPES = {"SECTION"}
NUMBERABLE_CONTAINER_TYPES = {
    "EVALUATION", "ADMIN_ENTRY", "OBSERVATION",
    "INSTRUCTION", "ACTION", "GENERIC_ENTRY", "CLUSTER"
}
_questionnaire_cache = None # Voor de getransformeerde structuur (gebruikt door CSV export)
_original_web_template_cache = None # Voor de ruwe template data (gebruikt door Medblocks UI)

# --- Functies voor Web Template Verwerking (Volledige Implementaties) ---

def get_original_web_template_data():
    global _original_web_template_cache
    if _original_web_template_cache is None:
        current_app.logger.info(f"CACHE MISS: Origineel Web Template. Laden vanaf: {TEMPLATE_FILEPATH}")
        try:
            with open(TEMPLATE_FILEPATH, 'r', encoding='utf-8') as f:
                _original_web_template_cache = json.load(f)
            current_app.logger.info(f"Origineel Web Template '{TEMPLATE_FILEPATH}' succesvol geladen en gecached.")
        except FileNotFoundError:
            current_app.logger.error(f"FOUT: Origineel Web Template niet gevonden: {TEMPLATE_FILEPATH}")
            flash(f"Kritiek: Web Template '{os.path.basename(TEMPLATE_FILEPATH)}' niet gevonden.", "danger")
            _original_web_template_cache = {} # Geef een leeg dict terug ipv None om verdere errors te voorkomen
        except json.JSONDecodeError as e:
            current_app.logger.error(f"FOUT: JSON parse error in {TEMPLATE_FILEPATH}: {e}")
            flash(f"Kritiek: Fout in Web Template JSON: {e}.", "danger")
            _original_web_template_cache = {}
        except Exception as e:
            current_app.logger.error(f"FOUT: Onverwachte error bij laden {TEMPLATE_FILEPATH}: {e}", exc_info=True)
            flash(f"Kritiek: Onverwachte fout Web Template: {e}", "danger")
            _original_web_template_cache = {}
    else:
        current_app.logger.info("CACHE HIT: Gebruik _original_web_template_cache uit cache.")
    return _original_web_template_cache

def _get_node_name(node_json, lang_codes=['nl', 'en']):
    # Volledige implementatie van _get_node_name zoals eerder gedeeld
    if not isinstance(node_json, dict): return "Ongeldige Node Structuur"
    if isinstance(node_json.get('localizedName'), str) and node_json['localizedName'].strip():
        return node_json['localizedName']
    localized_names_dict = node_json.get('localizedNames', {})
    if isinstance(localized_names_dict, dict):
        for lang_code in lang_codes:
            if lang_code in localized_names_dict and \
               isinstance(localized_names_dict[lang_code], str) and \
               localized_names_dict[lang_code].strip():
                return localized_names_dict[lang_code]
    if isinstance(node_json.get('name'), str) and node_json['name'].strip():
        return node_json['name']
    if isinstance(node_json.get('label'), str) and node_json['label'].strip():
        return node_json['label']
    node_id = node_json.get('id')
    if isinstance(node_id, str) and node_id.strip():
        return node_id 
    return "Naamloos Veld"

def _create_value_structure(node_json, lang_codes=['nl', 'en'], parent_semantic_path_for_options=""):
    """
    Creëert de waarde-structuur voor een gegeven node, inclusief het doorgeven van semantische paden.

    Args:
        node_json (dict): De JSON-definitie van de huidige node.
        lang_codes (list): Lijst van voorkeurstaalcodes.
        parent_semantic_path_for_options (str): Het semantische pad van de bovenliggende node,
                                                 gebruikt om paden voor geneste opties te vormen.
    Returns:
        dict: De geconstrueerde waarde-structuur.
    """
    default_value_structure = {"_type": "DV_TEXT", "value": f"[Onbekend Type: {node_json.get('rmType','GEEN RM TYPE')}]"}
    if not isinstance(node_json, dict):
        return default_value_structure

    node_rm_type = node_json.get('rmType', '').upper()
    input_def_list = node_json.get('inputs', [])
    input_def = input_def_list[0] if isinstance(input_def_list, list) and input_def_list else {}
    
    effective_data_type = node_rm_type if node_rm_type.startswith("DV_") else input_def.get('type', '').upper()

    # Specifieke behandeling voor ELEMENT nodes die een CHOICE van andere RM types bevatten
    # Dit zijn vaak keuzes tussen verschillende DV_ types of zelfs CLUSTERs.
    if node_rm_type == 'ELEMENT' and not effective_data_type.startswith("DV_"):
        node_children = node_json.get('children', []) # Dit zijn de JSON definities van de keuze-opties
        if node_children and all(isinstance(child, dict) for child in node_children):
            choice_options_list = [] # Hernoemd om verwarring met de uiteindelijke 'options' key te voorkomen
            for child_node_json_for_option in node_children: # child_node_json_for_option is de definitie van een optie
                option_name = _get_node_name(child_node_json_for_option, lang_codes)
                # archetype_node_id voor de optie zelf (kan een at-code zijn of een type)
                option_archetype_node_id = child_node_json_for_option.get('nodeId') or child_node_json_for_option.get('id')
                option_aql_path = child_node_json_for_option.get('aqlPath', '')
                
                # Bouw het semantische pad voor deze specifieke optie
                option_id_segment = child_node_json_for_option.get('id') # De 'id' van de optie-node
                current_option_semantic_path = parent_semantic_path_for_options # Begin met het pad van de parent
                
                if option_id_segment:
                    if parent_semantic_path_for_options:
                        # Voeg het ID-segment van de optie toe aan het pad van de parent.
                        # Dit is cruciaal als de 'id' van de child_node_json_for_option (de optie)
                        # daadwerkelijk een segment in het pad is (bv. .../parent_element_id/option_id)
                        # Als de 'id' van de optie niet direct een padsegment is (bv. 'at0001' voor een code),
                        # dan is het semantische pad voor de opmerking waarschijnlijk parent_semantic_path_for_options.
                        # De frontend bepaalt welk pad wordt gebruikt. Hier proberen we het zo specifiek mogelijk te maken.
                        # De logica in _flatten_leaf_nodes_for_export zal dit gebruiken.
                        current_option_semantic_path = f"{parent_semantic_path_for_options}/{option_id_segment}"
                    else:
                        current_option_semantic_path = option_id_segment
                
                # De 'value' van de optie zelf (bv. een DV_TEXT structuur, of een geneste DV_INTERVAL)
                # Geef het zojuist gevormde current_option_semantic_path door voor eventuele diepere structuren binnen de optie.
                option_value_data = _create_value_structure(child_node_json_for_option, lang_codes, parent_semantic_path_for_options=current_option_semantic_path)
                
                choice_options_list.append({
                    "_type": child_node_json_for_option.get('rmType'), # RM Type van de optie
                    "name": {"value": option_name},
                    "archetype_node_id": option_archetype_node_id, # Kan de 'code' zijn voor DV_CODED_TEXT opties of at-code
                    "aqlPath": option_aql_path,
                    "semantic_path": current_option_semantic_path, # Semantisch pad voor deze specifieke optie
                    "min": child_node_json_for_option.get('min'),
                    "max": child_node_json_for_option.get('max'),
                    "value": option_value_data, # De daadwerkelijke datastructuur van deze optie
                    "is_leaf": True # De optie zelf wordt als een 'blad' in de lijst van keuzes beschouwd
                })
            return {"_type": "CHOICE", "original_rm_type": node_rm_type, "options": choice_options_list, "value": None}
        
        # Als een ELEMENT geen DV_ type is en geen children heeft die opties zijn,
        # behandel het als een simpel tekstveld als fallback.
        effective_data_type = "DV_TEXT"

    # Standaard waarde-structuren voor diverse DV_ types
    value_structure = {}
    if effective_data_type in ['TEXT', 'STRING', 'DV_TEXT']:
        value_structure = {"_type": "DV_TEXT", "value": ""}
    elif effective_data_type in ['CODED_TEXT', 'DV_CODED_TEXT']:
        options_for_coded_text = [] # Dit zijn de voorgedefinieerde codes, niet de 'CHOICE' opties hierboven
        options_list_source = input_def.get('list', [])
        for option_item_json in options_list_source: # option_item_json is een {label, value, localizedLabels, ...} dict
            if isinstance(option_item_json, dict):
                label = _get_node_name(option_item_json, lang_codes)
                # De 'value' hier is de code string (bv. "at0002")
                # Deze opties krijgen geen eigen semantic_path; commentaar is op het DV_CODED_TEXT element zelf.
                options_for_coded_text.append({"label": label, "value": option_item_json.get('value')})
        value_structure = {
            "_type": "DV_CODED_TEXT", "value": "",
            "defining_code": {"code_string": "", "terminology_id": {"value": input_def.get('terminology', '')}},
            "options": options_for_coded_text # Dit zijn de codes, niet de structurele CHOICE opties
        }
    elif effective_data_type in ['BOOLEAN', 'DV_BOOLEAN']:
        value_structure = {"_type": "DV_BOOLEAN", "value": None}
    elif effective_data_type in ['INTEGER', 'COUNT', 'DV_COUNT']:
        value_structure = {"_type": "DV_COUNT", "magnitude": None}
    elif effective_data_type in ['DECIMAL', 'REAL', 'DOUBLE', 'DV_QUANTITY', 'QUANTITY']:
        units = input_def.get('units', '')
        validation = input_def.get('validation', {})
        if not units and validation and isinstance(validation.get('range'), dict):
            range_info = validation['range']
            if isinstance(range_info, list) and range_info:
                units = range_info[0].get('units', '')
            elif isinstance(range_info, dict):
                units = range_info.get('units', '')
        value_structure = {"_type": "DV_QUANTITY", "magnitude": None, "units": units}
    elif effective_data_type in ['DATETIME', 'DV_DATE_TIME']:
        value_structure = {"_type": "DV_DATE_TIME", "value": None}
    elif effective_data_type in ['DATE', 'DV_DATE']:
        value_structure = {"_type": "DV_DATE", "value": None}
    elif effective_data_type in ['TIME', 'DV_TIME']:
        value_structure = {"_type": "DV_TIME", "value": None}
    elif effective_data_type in ['IDENTIFIER', 'DV_IDENTIFIER']:
        value_structure = {"_type": "DV_IDENTIFIER", "id_value": "", "type": "", "issuer": "", "assigner": ""}
    elif effective_data_type in ['URI', 'DV_URI']:
        value_structure = {"_type": "DV_URI", "value": ""}
    elif effective_data_type.startswith("DV_INTERVAL"):
        inner_type_str = "DV_TEXT" # Default inner type
        match = re.search(r"DV_INTERVAL<([A-Z_]+)>", effective_data_type)
        if match:
            inner_type_str = match.group(1)
        
        lower_input_def, upper_input_def = {}, {}
        if isinstance(input_def_list, list):
            for sub_input in input_def_list:
                if sub_input.get("suffix") == "lower": lower_input_def = sub_input
                elif sub_input.get("suffix") == "upper": upper_input_def = sub_input
        
        if not lower_input_def and input_def: lower_input_def = input_def
        if not upper_input_def and input_def: upper_input_def = input_def

        # Creëer de JSON structuur voor de innerlijke 'lower' en 'upper' nodes
        lower_inner_node_json = {"rmType": inner_type_str, "inputs": [lower_input_def] if lower_input_def else []}
        upper_inner_node_json = {"rmType": inner_type_str, "inputs": [upper_input_def] if upper_input_def else []}
        
        # Bouw semantische paden voor lower en upper componenten
        lower_semantic_path = f"{parent_semantic_path_for_options}/lower" if parent_semantic_path_for_options else "lower"
        upper_semantic_path = f"{parent_semantic_path_for_options}/upper" if parent_semantic_path_for_options else "upper"

        value_structure = {
            "_type": "DV_INTERVAL",
            "lower": _create_value_structure(lower_inner_node_json, lang_codes, parent_semantic_path_for_options=lower_semantic_path),
            "upper": _create_value_structure(upper_inner_node_json, lang_codes, parent_semantic_path_for_options=upper_semantic_path),
            "lower_included": input_def.get("lower_included", True),
            "upper_included": input_def.get("upper_included", True)
        }
    elif effective_data_type == 'DV_DURATION':
        value_structure = {
            "_type": "DV_DURATION", "months": None, "weeks": None, "days": None,
            "hours": None, "minutes": None, "seconds": None
        }
    elif effective_data_type == 'DV_PROPORTION':
        prop_type = 0
        if input_def.get("list") and isinstance(input_def["list"], list) and len(input_def["list"]) > 0:
            try: prop_type = int(input_def["list"][0].get("value", 0))
            except ValueError: pass
        value_structure = {"_type": "DV_PROPORTION", "numerator": None, "denominator": None, "type": prop_type}
    else:
        current_app.logger.warning(f"WARN: Onbehandeld effective_data_type '{effective_data_type}' voor node '{node_json.get('id', 'unknown id')}' (rmType: {node_rm_type}). Gebruik default structuur.")
        return default_value_structure
            
    return value_structure

def _process_node_for_ui(current_node_json, lang_codes=['nl', 'en'], parent_aql_path="",
                         current_node_number_str="", current_node_level=-1, parent_semantic_path=""):
    if not isinstance(current_node_json, dict): return None

    node_rm_type = current_node_json.get('rmType', '').upper()
    aql_path = current_node_json.get('aqlPath', '')
    original_id_from_json = current_node_json.get('id') # ID van de huidige node, bv. "context", "relationship"

    # --- NIEUW: Bouw semantic_path ---
    current_id_segment = original_id_from_json
    full_semantic_path = parent_semantic_path # Standaard, als deze node geen eigen ID-segment heeft

    if current_id_segment: # Alleen toevoegen als er een ID-segment is voor deze node
        if parent_semantic_path:
            full_semantic_path = f"{parent_semantic_path}/{current_id_segment}"
        else:
            full_semantic_path = current_id_segment # Dit is het eerste segment
    # ---------------------------------

    relative_aql_path = aql_path.replace(parent_aql_path, '', 1).lstrip('/') if parent_aql_path else aql_path.lstrip('/')
    structural_metadata_names_to_skip = [
        'language', 'encoding', 'subject', 'category',
        'territory', 'composer', 'health_care_facility', 'location'
    ]
    is_structural_metadata = current_node_json.get('inContext') is True and \
                             relative_aql_path.count('/') == 0 and \
                             relative_aql_path in structural_metadata_names_to_skip
    if is_structural_metadata:
        return None

    node_name = _get_node_name(current_node_json, lang_codes)
    node_id_val = current_node_json.get('nodeId')
    display_id = node_id_val or original_id_from_json or 'uid_' + str(hash(aql_path))[-6:]
    min_occ = current_node_json.get('min')
    max_occ = current_node_json.get('max')

    is_leaf_element = False
    # ... (bestaande logica voor is_leaf_element blijft hetzelfde) ...
    if node_rm_type.startswith('DV_') and node_rm_type not in ['DV_INTERVAL']:
        is_leaf_element = True
    elif node_rm_type == 'ELEMENT':
        children_nodes = current_node_json.get('children', [])
        if not children_nodes and current_node_json.get('inputs'):
            is_leaf_element = True
        elif children_nodes and all(child.get('rmType','').upper().startswith('DV_') for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True
        elif not children_nodes and not current_node_json.get('inputs'):
            is_leaf_element = (not current_node_json.get('children') and not current_node_json.get('inputs')) or \
                              (current_node_json.get('children') and not any(c.get('rmType','').upper() in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}) for c in current_node_json.get('children',[]) if isinstance(c,dict)))
        elif not any(child.get('rmType','').upper() in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}) \
                                 for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True


    if is_leaf_element and (aql_path or full_semantic_path): # Moet een pad hebben
        # Belangrijk: _create_value_structure moet ook de semantic_path context krijgen als opties diep genest zijn
        # en hun eigen semantic_path segmenten moeten vormen. Voor nu gaan we ervan uit dat de options in
        # _create_value_structure geen verdere semantic path extensies nodig hebben *voorbij* full_semantic_path van het parent ELEMENT.
        # De opties zelf (de child_node_json in _create_value_structure) hebben hun eigen 'id' die daar gebruikt kan worden.
        
        # We moeten de `_create_value_structure` aanpassen om `full_semantic_path` door te geven aan zijn interne logica voor opties
        # als die opties ook een `semantic_path` moeten krijgen.
        # Voor nu, de options in de value structuur krijgen geen eigen `semantic_path` in dit voorbeeld,
        # de `semantic_path` hieronder is voor het element zelf.
        # De `opt.get('semantic_path')` in _flatten_leaf_nodes zal moeten komen van de `child_node_json` 'id' binnen `_create_value_structure`.

        leaf_data = {
            "_type": node_rm_type,
            "name": {"value": node_name},
            "archetype_node_id": display_id,
            "original_json_id": original_id_from_json, 
            "aqlPath": aql_path,
            "element_path_for_comments": aql_path, # Behoud AQL path voor eventueel ander gebruik
            "semantic_path": full_semantic_path,   # --- TOEGEVOEGD ---
            "min": min_occ,
            "max": max_occ,
            # Geef parent_semantic_path (hier full_semantic_path) door aan _create_value_structure
            "value": _create_value_structure(current_node_json, lang_codes, parent_semantic_path_for_options=full_semantic_path),
            "is_leaf": True
        }
        if current_node_level >= 0:
            leaf_data['level'] = current_node_level
        return leaf_data

    is_displayable_container_type = node_rm_type in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'})
    if is_displayable_container_type:
        processed_children = []
        json_children_list = current_node_json.get('children', [])
        child_numbering_counter = 0
        if isinstance(json_children_list, list):
            for child_idx, child_json_node in enumerate(json_children_list):
                if not isinstance(child_json_node, dict): continue
                child_rm_type = child_json_node.get('rmType', '').upper()
                child_full_number_str = ""
                child_level = current_node_level + 1 if current_node_level != -1 else 0
                if current_node_number_str and child_rm_type in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}):
                    child_numbering_counter += 1
                    child_full_number_str = f"{current_node_number_str}.{child_numbering_counter}"
                
                processed_child_node = _process_node_for_ui(
                    child_json_node,
                    lang_codes,
                    aql_path, # parent_aql_path voor het kind
                    current_node_number_str=child_full_number_str,
                    current_node_level=child_level,
                    parent_semantic_path=full_semantic_path # --- TOEGEVOEGD ---
                )
                # ... (rest van de logica voor processed_children) ...
                if processed_child_node:
                    if isinstance(processed_child_node, list):
                        for sub_child in processed_child_node:
                            if isinstance(sub_child, dict) and 'level' not in sub_child:
                                sub_child['level'] = child_level
                            processed_children.append(sub_child)
                    elif isinstance(processed_child_node, dict):
                        if 'level' not in processed_child_node : processed_child_node['level'] = child_level
                        if child_full_number_str and 'section_number' not in processed_child_node and \
                           processed_child_node.get('_type') in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}):
                            processed_child_node['section_number'] = child_full_number_str
                        processed_children.append(processed_child_node)


        # Alleen een node teruggeven als het een AQL-pad heeft (of semantisch pad) EN kinderen
        # Of als het de root call is die alleen kinderen mag teruggeven (impliciet)
        if (aql_path or full_semantic_path): # Containers moeten een of ander pad hebben
            node_data = {
                "_type": node_rm_type,
                "name": {"value": node_name},
                "archetype_node_id": display_id,
                "original_json_id": original_id_from_json,
                "min": min_occ,
                "max": max_occ,
                "aqlPath": aql_path,
                "semantic_path": full_semantic_path, # --- TOEGEVOEGD ---
                "children": processed_children,
                "is_leaf": False
            }
            if current_node_level != -1:
                node_data['level'] = current_node_level
            if current_node_number_str:
                node_data['section_number'] = current_node_number_str
            return node_data
        elif processed_children: # Als het een anonieme root-achtige container is zonder eigen pad
             return processed_children


    # Fallback voor andere typen nodes die kinderen kunnen hebben maar niet "displayable containers" zijn
    if isinstance(current_node_json.get('children'), list):
        fallback_elements = []
        for child_json in current_node_json.get('children', []):
            processed_child = _process_node_for_ui(
                child_json,
                lang_codes,
                parent_aql_path, # Blijft parent_aql_path van de huidige node
                current_node_number_str=current_node_number_str, # Nummering niet voortzetten
                current_node_level=current_node_level, # Level niet per se verhogen
                parent_semantic_path=full_semantic_path # --- TOEGEVOEGD ---
            )
            if processed_child:
                if isinstance(processed_child, list): fallback_elements.extend(p for p in processed_child if p)
                else: fallback_elements.append(processed_child)
        if fallback_elements: return fallback_elements
            
    return None

def transform_web_template_to_questionnaire(web_template_data):
    if not web_template_data or not isinstance(web_template_data.get('tree'), dict):
        return {"_type": "COMPOSITION", "name": {"value": "Fout: Template 'tree' ongeldig"}, "version": "", "content": []}

    # --- NIEUW: Haal de root template ID op voor de semantic path ---
    # Het pad dat je gaf begint met 'individueel_zorgplan_palliatieve_zorg'. Dit is waarschijnlijk de templateId.
    root_template_id = web_template_data.get('templateId', web_template_data.get('id', 'unknown_template'))
    # Als de templateId niet direct het gewenste prefix is, moet je dit aanpassen.
    # Bijvoorbeeld, als de templateId 'ACP-DUTCH' is maar de paden beginnen met iets anders.
    # Voor nu nemen we aan dat `root_template_id` het eerste deel van je semantic path is.
    # Echter, het pad dat je gaf begint met 'individueel_zorgplan_palliatieve_zorg', wat een specifieke template naam lijkt.
    # Laten we aannemen dat de `id` van de `tree` node (de COMPOSITION) dit is, of anders de templateId.
    # De `current_node_json.get('id')` in _process_node_for_ui zal de segmenten toevoegen.
    # Dus de eerste `parent_semantic_path` zou leeg kunnen zijn, en de `id` van de COMPOSITION node vormt het eerste segment.
    # Of, als de COMPOSITION geen 'id' heeft, en de top-level sections wel, dan beginnen de paden met die section 'id's.
    #
    # Jouw pad: individueel_zorgplan_palliatieve_zorg/context/wettelijk_vertegenwoordiger_contactpersoon/relationship
    # 'individueel_zorgplan_palliatieve_zorg' is waarschijnlijk de `id` van de COMPOSITION node (root_tree).
    # Dus de eerste `parent_semantic_path` voor de kinderen van root_tree moet de `id` van root_tree zijn.

    initial_parent_semantic_path = web_template_data.get('tree', {}).get('id', root_template_id)
    # Als tree.id niet bestaat, gebruik root_template_id. Als die ook niet relevant is, start leeg
    # en laat de eerste _process_node_for_ui call het eerste segment bepalen.
    # Voor jouw pad lijkt het erop dat `initial_parent_semantic_path` de `id` van de root COMPOSITION node moet zijn.
    # Als de root tree `id` bijvoorbeeld `ACP_zorgplan` is, en de templateId `individueel_zorgplan_palliatieve_zorg`,
    # en het pad dat Medblocks gebruikt is `individueel_zorgplan_palliatieve_zorg/context/...`, dan moet
    # `initial_parent_semantic_path` = "individueel_zorgplan_palliatieve_zorg" zijn.
    # Dit kan handmatig worden ingesteld of uit een specifieke template eigenschap worden gehaald.
    # Laten we voor nu aannemen dat de root_tree een 'id' heeft die het eerste deel van het pad is.
    # Of dat we de templateId als basis gebruiken.

    # De `id` van de `root_tree` (de COMPOSITION node)
    composition_id = web_template_data.get('tree', {}).get('id')
    # Als de `id` van de compositie niet `individueel_zorgplan_palliatieve_zorg` is, moet de `base_path_for_comments` anders worden ingesteld.
    # Voor nu gaan we ervan uit dat `composition_id` het eerste deel van je pad is.
    # Als de `pathElement.getAttribute('path')` in JS niet start met de composition ID, maar bv. direct met `context/...`,
    # dan moet `initial_parent_semantic_path` leeg zijn.
    # Gezien jouw pad `individueel_zorgplan_palliatieve_zorg/...`, is dit waarschijnlijk de `templateId`.
    # Laten we die als basis nemen voor de kinderen.

    base_semantic_path_for_content = web_template_data.get('templateId', 'default_template') 
    # Pas dit aan als 'individueel_zorgplan_palliatieve_zorg' ergens anders vandaan komt (bv. `tree.id`)

    # ... (bestaande code voor default_lang, composition_name etc.) ...
    default_lang = web_template_data.get('defaultLanguage', 'nl')
    available_langs = web_template_data.get('languages', ['nl', 'en'])
    pref_langs = []
    if 'nl' in available_langs: pref_langs.append('nl')
    if default_lang not in pref_langs and default_lang in available_langs: pref_langs.append(default_lang)
    for lang in available_langs:
        if lang not in pref_langs: pref_langs.append(lang)
    if not pref_langs: pref_langs = ['nl', 'en']

    root_tree = web_template_data['tree']
    composition_name = _get_node_name(root_tree, pref_langs)
    composition_version = web_template_data.get('version', web_template_data.get('semVer', ''))
    composition_archetype_id = root_tree.get('nodeId', root_tree.get('id', 'root_id_onbekend'))
    # Semantic path voor de compositie zelf
    composition_semantic_path = root_tree.get('id', base_semantic_path_for_content) # Het eerste deel van het pad.

    content_list_for_flask = []
    top_level_section_counter = 0
    if isinstance(root_tree.get('children'), list):
        for idx, top_level_json_node in enumerate(root_tree.get('children', [])): # Dit zijn de SECTIONS, EVALUATIONS etc.
            if not isinstance(top_level_json_node, dict): continue
            
            top_level_rm_type = top_level_json_node.get('rmType', '').upper()
            is_main_section_type = top_level_rm_type in ORIGINAL_SECTION_TYPES or \
                                   (top_level_rm_type == 'EVENT_CONTEXT' and top_level_json_node.get('id') == 'context')

            if is_main_section_type:
                top_level_section_counter += 1
                current_section_number_str = str(top_level_section_counter)
                current_level_int = 0
                hoofdstuk_naam = _get_node_name(top_level_json_node, pref_langs)
                if top_level_rm_type == 'EVENT_CONTEXT' and hoofdstuk_naam in ["Event Context", "Naamloos Veld", "context"]:
                    hoofdstuk_naam = "Context"
                top_level_aql_path = top_level_json_node.get('aqlPath')
                if not top_level_aql_path:
                    node_id_for_aql = top_level_json_node.get('nodeId') or top_level_json_node.get('id', f'generated_section_id_{idx}')
                    top_level_aql_path = f"/content[openEHR-EHR-SECTION.adhoc.v1 and name/value='{node_id_for_aql}']"
                    current_app.logger.warning(f"WARN: Hoofdsectie '{hoofdstuk_naam}' heeft geen aqlPath. Fallback: {top_level_aql_path}")

                # Semantic path voor deze hoofdsectie
                section_id_segment = top_level_json_node.get('id')
                current_main_section_semantic_path = composition_semantic_path # Standaard
                if section_id_segment:
                    if composition_semantic_path : # Als de compositie al een ID-pad segment had
                         current_main_section_semantic_path = f"{composition_semantic_path}/{section_id_segment}"
                    else: # Als de sectie het eerste ID-segment is
                         current_main_section_semantic_path = section_id_segment
                
                main_section_node = {
                    "_type": top_level_rm_type, "name": {"value": hoofdstuk_naam},
                    "archetype_node_id": top_level_json_node.get('nodeId') or top_level_json_node.get('id'),
                    "aqlPath": top_level_aql_path, 
                    "semantic_path": current_main_section_semantic_path, # --- TOEGEVOEGD ---
                    "min": top_level_json_node.get('min'),
                    "max": top_level_json_node.get('max'), "children": [],
                    "is_leaf": False, "level": current_level_int,
                    "section_number": current_section_number_str
                }
                processed_children_list = []
                if isinstance(top_level_json_node.get('children'), list):
                    child_numbering_counter = 0
                    for child_json in top_level_json_node.get('children', []):
                        if not isinstance(child_json, dict): continue
                        child_rm_type = child_json.get('rmType', '').upper()
                        child_full_number_str = ""
                        if current_section_number_str and child_rm_type in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}):
                            child_numbering_counter += 1
                            child_full_number_str = f"{current_section_number_str}.{child_numbering_counter}"
                        
                        # Geef het semantic_path van de huidige hoofdsectie door als parent
                        processed_child = _process_node_for_ui(
                            child_json, pref_langs, parent_aql_path=top_level_aql_path,
                            current_node_number_str=child_full_number_str, current_node_level=current_level_int + 1,
                            parent_semantic_path=current_main_section_semantic_path # --- TOEGEVOEGD ---
                        )
                        if processed_child:
                            if isinstance(processed_child, list):
                                processed_children_list.extend(p for p in processed_child if p)
                            else:
                                processed_children_list.append(processed_child)
                main_section_node['children'] = processed_children_list
                content_list_for_flask.append(main_section_node)
            else:
                initial_level_for_other_types = 0 if top_level_rm_type in NUMBERABLE_CONTAINER_TYPES else -1
                # Voor andere top-level items, de parent_semantic_path is nog steeds die van de compositie
                processed_node = _process_node_for_ui(
                    top_level_json_node, pref_langs, parent_aql_path=root_tree.get('aqlPath',''),
                    current_node_number_str="", current_node_level=initial_level_for_other_types,
                    parent_semantic_path=composition_semantic_path # --- TOEGEVOEGD ---
                )
                # ... (rest van de logica voor processed_node) ...
                if processed_node:
                    if isinstance(processed_node, dict):
                        if 'level' not in processed_node and initial_level_for_other_types != -1 :
                            processed_node['level'] = initial_level_for_other_types
                        content_list_for_flask.append(processed_node)
                    elif isinstance(processed_node, list):
                        content_list_for_flask.extend(p for p in processed_node if p)

    return {
        "_type": "COMPOSITION", "name": {"value": composition_name},
        "version": composition_version, "archetype_node_id": composition_archetype_id,
        "semantic_path": composition_semantic_path, # --- TOEGEVOEGD ---
        "content": content_list_for_flask
    }

def get_cached_questionnaire_structure():
    global _questionnaire_cache
    if _questionnaire_cache is None:
        current_app.logger.info("CACHE MISS: Getransformeerde vragenlijst. Bezig met laden en transformeren...")
        web_template_data = get_original_web_template_data() 
        if web_template_data and web_template_data != {}: 
            _questionnaire_cache = transform_web_template_to_questionnaire(web_template_data)
            if not _questionnaire_cache.get("content"):
                 current_app.logger.warning("Getransformeerde vragenlijst heeft lege 'content'.")
        else:
            _questionnaire_cache = {
                "_type": "COMPOSITION", "name": {"value": "FOUT: Template kon niet geladen of verwerkt worden."}, 
                "version": "", "content": []
            }
    else:
        current_app.logger.info("CACHE HIT: Gebruik _questionnaire_cache (getransformeerd) uit cache.")
    return _questionnaire_cache

def _collect_element_paths(node_or_list):
    paths = set()
    if isinstance(node_or_list, list):
        for item in node_or_list: paths.update(_collect_element_paths(item))
    elif isinstance(node_or_list, dict):
        node = node_or_list
        path_key = node.get('element_path_for_comments') or node.get('aqlPath')
        if path_key: paths.add(path_key) 
        if isinstance(node.get('value'), dict) and node['value'].get('_type') == 'CHOICE':
            for option in node['value'].get('options', []):
                if isinstance(option, dict) and option.get('aqlPath'): paths.add(option['aqlPath'])
        if 'children' in node and isinstance(node['children'], list):
            for child in node['children']: paths.update(_collect_element_paths(child))
    return paths

def _flatten_leaf_nodes_for_export(node, leaf_nodes_dict):
    if isinstance(node, dict):
        # Haal het semantische pad op dat door _process_node_for_ui is toegevoegd
        current_node_semantic_path = node.get('semantic_path')

        if node.get('is_leaf') and current_node_semantic_path: # Gebruik semantic_path
            element_name = node.get('name', {}).get('value', 'Naamloos')
            # Node ID voor CSV is nog steeds archetype_node_id van het element (zoals in logs)
            element_node_id_for_csv = node.get('archetype_node_id', 'Geen ID')

            if node.get('value') and isinstance(node['value'], dict) and node['value'].get('_type') == 'CHOICE':
                options = node['value'].get('options', [])
                if not options:
                    # Gebruik semantic_path van het element als key en comment_path
                    leaf_nodes_dict[current_node_semantic_path] = {
                        'csv_name': element_name,
                        'csv_node_id': element_node_id_for_csv,
                        'comment_path': current_node_semantic_path
                    }
                else:
                    for i, opt in enumerate(options):
                        if isinstance(opt, dict):
                            # Opties hebben nu ook een 'semantic_path' veld door aanpassing in _create_value_structure
                            option_specific_semantic_path = opt.get('semantic_path')
                            
                            # Als een optie geen eigen semantic_path heeft (bv. geen 'id'),
                            # dan is de opmerking waarschijnlijk bedoeld voor het parent element.
                            # Of we moeten een manier hebben om de optie uniek te identificeren binnen de semantic path.
                            # Voor nu: als de optie een eigen semantic path heeft, gebruik dat. Anders, het pad van het element.
                            # Jouw pad `.../relationship` suggereert dat 'relationship' de 'id' is en dus deel van semantic path.
                            # Als 'relationship' een DV_CODED_TEXT is, zijn de opties (codes) meestal niet aparte ID-segmenten in zo'n pad.
                            # De opmerking is dan waarschijnlijk op '.../relationship' zelf.

                            comment_path_for_option_row = option_specific_semantic_path or current_node_semantic_path

                            # Naamgeving voor CSV (behoud originele logica die elementnaam en optie-ID combineert)
                            option_value_id_for_name = opt.get('archetype_node_id', '') # ID/code van de optie
                            csv_name_for_option = f"{element_name} ({option_value_id_for_name})" \
                                if i > 0 and option_value_id_for_name and option_value_id_for_name.lower() != 'value' \
                                else (f"{element_name} (als {opt.get('value',{}).get('_type',f'Optie{i+1}')})" \
                                if i > 0 else element_name)
                            
                            # De unieke sleutel voor all_questions_map.
                            # Als we overstappen op semantic_path als primaire identificatie,
                            # zou de key idealiter `comment_path_for_option_row` moeten zijn,
                            # mogelijk aangevuld om uniekheid te garanderen als meerdere opties dezelfde path zouden krijgen.
                            # Voor nu houden we de oude #CHOICE_OPT structuur voor de dict key, maar vullen met semantic path.
                            # Echter, als `comment_path_for_option_row` de unieke identifier wordt,
                            # dan moet de `all_questions_map` daarmee geïndexeerd worden.
                            # Laten we `comment_path_for_option_row` als sleutel gebruiken als die er is.
                            dict_key_for_map = comment_path_for_option_row
                            if not dict_key_for_map : # Fallback als semantic path ontbreekt
                                dict_key_for_map = f"{current_node_semantic_path or node.get('aqlPath')}#CHOICE_OPT_{i}_{option_value_id_for_name or i}"


                            leaf_nodes_dict[dict_key_for_map] = {
                                'csv_name': csv_name_for_option,
                                'csv_node_id': element_node_id_for_csv, # Nog steeds Node ID van het parent element
                                'comment_path': comment_path_for_option_row
                            }
            else: # Regulier blad-element
                # Gebruik semantic_path als key en comment_path
                leaf_nodes_dict[current_node_semantic_path] = {
                    'csv_name': element_name,
                    'csv_node_id': element_node_id_for_csv,
                    'comment_path': current_node_semantic_path
                }

        # Recursie voor kinderen van container nodes (als node zelf geen blad is)
        if 'children' in node and isinstance(node.get('children'), list) and not node.get('is_leaf'):
            for child_node in node.get('children', []):
                _flatten_leaf_nodes_for_export(child_node, leaf_nodes_dict) # child_node heeft zijn eigen semantic_path

    elif isinstance(node, list):
        for item_node in node:
            _flatten_leaf_nodes_for_export(item_node, leaf_nodes_dict)
            
# --- Routes ---
@bp.route('/')
def index():
    # AANGEPAST: Redirect naar de Medblocks-compatibele form_page zonder section_index
    return redirect(url_for('main.form_page'))

@bp.route('/formulier', methods=['GET']) # AANGEPAST: Geen section_index meer
def form_page():
    original_web_template_dict_for_mb = get_original_web_template_data()
    questionnaire_transformed_structure = get_cached_questionnaire_structure() 
    error_msg_for_template = None
    if not original_web_template_dict_for_mb or original_web_template_dict_for_mb == {}:
        error_msg_for_template = "Kritieke fout: De web template definitie (ACP-DUTCH.json) kon niet geladen worden."
        questionnaire_name = "Fout bij laden vragenlijst"
    else:
        questionnaire_name = questionnaire_transformed_structure.get('name',{}).get('value','Vragenlijst')
    
    # De comments_by_path variabele wordt hier niet meer meegegeven aan de template,
    # omdat de API endpoints gebruikt zullen worden door de frontend.
    return render_template('index.html',
                           title=f"Review: {questionnaire_name}",
                           web_template_for_mb_js=original_web_template_dict_for_mb, 
                           questionnaire_for_js=questionnaire_transformed_structure, 
                           error_message=error_msg_for_template)

@bp.route('/submit-openehr-data', methods=['POST'])
def submit_openehr_data():
    if not request.is_json:
        return jsonify({"error": "Request moet JSON zijn", "status": "error"}), 400
    data = request.get_json()
    current_app.logger.info(f"Ontvangen Medblocks UI data: {json.dumps(data, indent=2, ensure_ascii=False)}")
    return jsonify({"message": "Data succesvol ontvangen (simulatie)", "status": "success"}), 200

@bp.route('/export/comments')
def export_comments_csv():
    try:
        current_app.logger.info("Start genereren CSV-export voor commentaren (per vraag)...")
        questionnaire = get_cached_questionnaire_structure()
        all_questions_map = {}
        _flatten_leaf_nodes_for_export(questionnaire.get('content', []), all_questions_map)
        current_app.logger.info(f"Aantal unieke 'vragen' (items in all_questions_map): {len(all_questions_map)}")

        all_comments_db = Comment.query.order_by(Comment.element_path, Comment.created_at.asc()).all()
        current_app.logger.info(f"Totaal aantal commentaren opgehaald uit DB: {len(all_comments_db)}")

        comments_by_element_path = defaultdict(list)
        if not all_comments_db:
            current_app.logger.info("Geen commentaren gevonden in de database.")
        else:
            for comment_obj in all_comments_db:
                cleaned_comment_text = comment_obj.comment_text.replace('\r', '').replace('\n', ' ')
                comments_by_element_path[comment_obj.element_path].append(cleaned_comment_text)
                # Log een paar voorbeelden van geladen commentaren en hun paden
                if len(comments_by_element_path) <= 5: # Log details voor de eerste paar paden
                     current_app.logger.debug(f"Commentaar voor pad '{comment_obj.element_path}': '{cleaned_comment_text}'")
        current_app.logger.info(f"Commentaren gegroepeerd per element_path. Aantal paden met commentaren: {len(comments_by_element_path)}")
        # Optioneel: log alle gegroepeerde commentaren als het er niet te veel zijn
        # current_app.logger.debug(f"Gegroepeerde commentaren: {comments_by_element_path}")


        output = io.StringIO()
        writer = csv.writer(output, quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(['Vraag Naam', 'Node ID', 'Commentaar'])

        rows_written = 0
        for question_key, question_details in all_questions_map.items(): # Itereer over items om ook de key te hebben voor logging
            vraag_naam = question_details.get('csv_name', 'N.v.t.')
            node_id_at_code = question_details.get('csv_node_id', 'N.v.t.')
            comment_path_for_this_question = question_details.get('comment_path')

            # Logging voor elke vraag in de CSV
            current_app.logger.debug(f"--- Verwerken voor CSV-rij ---")
            current_app.logger.debug(f"Vraag Key (uit all_questions_map): '{question_key}'")
            current_app.logger.debug(f"Vraag Naam: '{vraag_naam}', Node ID: '{node_id_at_code}'")
            current_app.logger.debug(f"Verwacht comment_path voor deze vraag: '{comment_path_for_this_question}'")

            associated_comments_list = []
            if comment_path_for_this_question:
                associated_comments_list = comments_by_element_path.get(comment_path_for_this_question, [])
                if not associated_comments_list:
                    current_app.logger.debug(f"GEEN commentaren gevonden in 'comments_by_element_path' voor pad: '{comment_path_for_this_question}'")
                else:
                    current_app.logger.debug(f"GEVONDEN commentaren voor pad '{comment_path_for_this_question}': {associated_comments_list}")
            else:
                current_app.logger.warning(f"Geen 'comment_path' gedefinieerd voor vraag: '{vraag_naam}' (key: {question_key})")
            
            concatenated_comments = "; ".join(associated_comments_list)
            # current_app.logger.debug(f"Samengevoegde commentaren voor CSV: '{concatenated_comments}'") # Kan veel output geven

            writer.writerow([vraag_naam, node_id_at_code, concatenated_comments])
            rows_written += 1
        
        current_app.logger.info(f"Totaal aantal rijen geschreven naar CSV: {rows_written}")

        output.seek(0)
        current_app.logger.info("CSV-export succesvol gegenereerd.")
        return Response(
            output, mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=commentaren_export_per_vraag.csv"}
        )
    except Exception as e:
        current_app.logger.error(f"FATALE Fout bij het genereren van CSV-export (per vraag): {e}", exc_info=True)
        flash(f'Fout bij het genereren van CSV-export (per vraag): {str(e)}', 'danger')
        referrer_url = request.referrer
        if not referrer_url:
            try:
                referrer_url = url_for('main.form_page') 
            except Exception:
                 referrer_url = url_for('/')
        return redirect(referrer_url)

# --- API Endpoints voor Commentaren (voor Medblocks UI integratie) ---

@bp.route('/api/comments/get/<path:element_path>', methods=['GET'])
def get_comments_api(element_path):
    comments = Comment.query.filter_by(element_path=element_path).order_by(Comment.created_at.asc()).all()
    comments_data = []
    for comment in comments:
        comments_data.append({
            'id': comment.id, 'comment_text': comment.comment_text,
            'author_name': comment.author_name,
            'created_at': comment.created_at.isoformat() + 'Z' 
        })
    return jsonify(comments_data)

@bp.route('/api/comments/add', methods=['POST'])
def add_comment_api():
    comment_text = request.form.get('comment_text')
    author_name = request.form.get('author_name', 'Anoniem').strip()
    element_path = request.form.get('element_path')
    if not author_name: author_name = 'Anoniem'
    if not comment_text or not comment_text.strip():
        return jsonify({"status": "error", "message": "Commentaartekst mag niet leeg zijn."}), 400
    if not element_path:
        return jsonify({"status": "error", "message": "Element pad (element_path) is verplicht."}), 400
    try:
        timestamp = datetime.now(pytz.utc) 
        comment = Comment(
            comment_text=comment_text.strip(), author_name=author_name,
            element_path=element_path, created_at=timestamp
        )
        db.session.add(comment)
        db.session.commit()
        current_app.logger.info(f"API: Commentaar succesvol opgeslagen voor pad '{element_path}' door '{author_name}'")
        return jsonify({
            "status": "success", "message": "Opmerking succesvol geplaatst!",
            "comment": {
                'id': comment.id, 'comment_text': comment.comment_text,
                'author_name': comment.author_name,
                'created_at': comment.created_at.isoformat() + 'Z',
                'element_path': comment.element_path
            }
        }), 201
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"API Fout bij plaatsen commentaar voor pad '{element_path}': {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Serverfout bij het plaatsen van uw opmerking: {str(e)}"}), 500

@bp.route('/api/comments/update/<int:comment_id>', methods=['PUT'])
def update_comment_api(comment_id):
    comment_to_update = Comment.query.get_or_404(comment_id) # Haalt comment op of geeft 404 als niet gevonden
    
    new_comment_text = request.form.get('comment_text')


    if not new_comment_text or not new_comment_text.strip():
        return jsonify({"status": "error", "message": "Commentaartekst mag niet leeg zijn."}), 400

    
    comment_to_update.comment_text = new_comment_text.strip()


    try:
        db.session.commit()
        current_app.logger.info(f"API: Commentaar ID {comment_id} succesvol bijgewerkt.")
        return jsonify({
            "status": "success", 
            "message": "Opmerking succesvol bijgewerkt!",
            "comment": {
                'id': comment_to_update.id, 
                'comment_text': comment_to_update.comment_text,
                'author_name': comment_to_update.author_name,
                'created_at': comment_to_update.created_at.isoformat() + 'Z', # Blijft de originele creatiedatum
                'element_path': comment_to_update.element_path
                # Voeg 'updated_at' toe als je dat veld gebruikt
            }
        }), 200
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f"API Fout bij bijwerken commentaar ID {comment_id}: {e}", exc_info=True)
        return jsonify({"status": "error", "message": f"Serverfout bij het bijwerken van uw opmerking: {str(e)}"}), 500


