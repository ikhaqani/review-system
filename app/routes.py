# app/main/routes.py

from flask import render_template, flash, redirect, url_for, Blueprint, request, abort, current_app, Response
from app import db
from app.models import Comment
import json
import os
from datetime import datetime
import pytz
import io
import csv

bp = Blueprint('main', __name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_FILEPATH = os.path.join(BASE_DIR, 'templates_openehr', 'ACP-DUTCH.json')

# Definieer de rmTypes per niveau voor de nummering
ORIGINAL_SECTION_TYPES = {"SECTION"}
NUMBERABLE_CONTAINER_TYPES = {"EVALUATION", "ADMIN_ENTRY", "OBSERVATION", "INSTRUCTION", "ACTION", "GENERIC_ENTRY", "CLUSTER"}
# EVENT_CONTEXT wordt speciaal behandeld in transform_web_template_to_questionnaire

_questionnaire_cache = None

def load_web_template_json(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            template_data = json.load(f)
        print(f"INFO: Template bestand '{filepath}' succesvol geladen.")
        return template_data
    except FileNotFoundError:
        print(f"FOUT: Bestand niet gevonden: {filepath}")
        flash(f"Template bestand niet gevonden: {filepath}. Controleer het pad.", "danger")
        return None
    except json.JSONDecodeError as e:
        print(f"FOUT: Fout bij het parsen van JSON in {filepath}: {e}")
        flash(f"Fout in template JSON-bestand: {e}. Controleer de JSON-syntax.", "danger")
        return None
    except Exception as e:
        print(f"FOUT: Onverwachte fout bij laden/parsen van {filepath}: {e}")
        flash(f"Onverwachte fout bij verwerken template: {e}", "danger")
        return None

def _get_node_name(node_json, lang_codes=['nl', 'en']):
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

def _create_value_structure(node_json, lang_codes=['nl', 'en']):
    default_value_structure = {"_type": "DV_TEXT", "value": f"[Onbekend Type: {node_json.get('rmType','GEEN RM TYPE')}]"}
    if not isinstance(node_json, dict): return default_value_structure

    node_rm_type = node_json.get('rmType', '').upper()
    input_def_list = node_json.get('inputs', [])
    input_def = input_def_list[0] if isinstance(input_def_list, list) and input_def_list else {}
    
    effective_data_type = node_rm_type if node_rm_type.startswith("DV_") else input_def.get('type', '').upper()

    if node_rm_type == 'ELEMENT' and not effective_data_type.startswith("DV_"):
        node_children = node_json.get('children', [])
        if node_children and all(isinstance(child, dict) for child in node_children):
            choice_options = []
            for child_node_json in node_children:
                option_name = _get_node_name(child_node_json, lang_codes)
                option_archetype_node_id = child_node_json.get('nodeId') or child_node_json.get('id')
                option_aql_path = child_node_json.get('aqlPath', '')
                option_value_data = _create_value_structure(child_node_json, lang_codes) 

                choice_options.append({
                    "_type": child_node_json.get('rmType'), 
                    "name": {"value": option_name},
                    "archetype_node_id": option_archetype_node_id,
                    "aqlPath": option_aql_path, 
                    "min": child_node_json.get('min'),
                    "max": child_node_json.get('max'),
                    "value": option_value_data, 
                    "is_leaf": True 
                })
            return {
                "_type": "CHOICE", 
                "original_rm_type": node_rm_type, 
                "options": choice_options,
                "value": None 
            }
        effective_data_type = "DV_TEXT" 

    value_structure = {}
    if effective_data_type in ['TEXT', 'STRING', 'DV_TEXT']:
        value_structure = {"_type": "DV_TEXT", "value": ""}
    elif effective_data_type in ['CODED_TEXT', 'DV_CODED_TEXT']:
        options = []
        options_list_source = input_def.get('list', [])
        for option_item in options_list_source:
            if isinstance(option_item, dict):
                label = _get_node_name(option_item, lang_codes) 
                options.append({"label": label, "value": option_item.get('value')})
        value_structure = {
            "_type": "DV_CODED_TEXT", "value": "", 
            "defining_code": {"code_string": "", "terminology_id": {"value": input_def.get('terminology', '')}},
            "options": options
        }
    elif effective_data_type in ['BOOLEAN', 'DV_BOOLEAN']:
        value_structure = {"_type": "DV_BOOLEAN", "value": None} 
    elif effective_data_type in ['INTEGER', 'COUNT', 'DV_COUNT']:
        value_structure = {"_type": "DV_COUNT", "magnitude": None}
    elif effective_data_type in ['DECIMAL', 'REAL', 'DOUBLE', 'DV_QUANTITY', 'QUANTITY']:
        units = input_def.get('units', '')
        validation_range = input_def.get('validation', {}).get('range', {})
        if not units and isinstance(validation_range.get('units'), str) : units = validation_range['units']
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
    elif effective_data_type.startswith('DV_INTERVAL') or effective_data_type in ['DV_DURATION', 'DV_PROPORTION']:
        value_structure = {"_type": effective_data_type, "value": f"[{effective_data_type.replace('DV_', '')} placeholder]"}
    else:
        print(f"WARN: Onbehandeld effective_data_type '{effective_data_type}' voor node '{node_json.get('id', 'unknown id')}' (rmType: {node_rm_type}). Gebruik default structuur.")
        return default_value_structure
        
    return value_structure

def _process_node_for_ui(current_node_json, lang_codes=['nl', 'en'], parent_aql_path="", current_node_number_str="", current_node_level=-1):
    if not isinstance(current_node_json, dict): return None
    
    node_rm_type = current_node_json.get('rmType', '').upper()
    aql_path = current_node_json.get('aqlPath', '')

    relative_aql_path = aql_path.replace(parent_aql_path, '', 1).lstrip('/') if parent_aql_path else aql_path.lstrip('/')
    is_structural_metadata = current_node_json.get('inContext') is True and \
                             (relative_aql_path.count('/') == 0 and \
                              relative_aql_path in ['language', 'encoding', 'subject', 'category', 'territory', 'composer', 'setting', 'start_time', 'health_care_facility', 'location', 'other_context'])
    if is_structural_metadata:
        return None

    node_name = _get_node_name(current_node_json, lang_codes)
    
    node_id_val = current_node_json.get('nodeId') 
    id_val = current_node_json.get('id')         
    display_id = node_id_val or id_val or 'uid_' + str(hash(aql_path))[-6:]
    
    min_occ = current_node_json.get('min')
    max_occ = current_node_json.get('max')

    is_leaf_element = False
    if node_rm_type.startswith('DV_') and node_rm_type not in ['DV_INTERVAL']: 
        is_leaf_element = True
    elif node_rm_type == 'ELEMENT':
        children_nodes = current_node_json.get('children', [])
        if not children_nodes and current_node_json.get('inputs'): 
            is_leaf_element = True
        elif children_nodes and all(child.get('rmType','').upper().startswith('DV_') for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True 
        elif not children_nodes and not current_node_json.get('inputs'): 
            is_leaf_element = False 
        elif not any(child.get('rmType','').upper() in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}) \
                     for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True

    if is_leaf_element and aql_path: 
        leaf_data = {
            "_type": node_rm_type, 
            "name": {"value": node_name}, 
            "archetype_node_id": display_id,
            "aqlPath": aql_path, 
            "element_path_for_comments": aql_path,
            "min": min_occ, 
            "max": max_occ,
            "value": _create_value_structure(current_node_json, lang_codes),
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
                    aql_path, 
                    current_node_number_str=child_full_number_str, 
                    current_node_level=child_level
                )
                
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
        
        if aql_path:
            node_data = {
                "_type": node_rm_type, 
                "name": {"value": node_name}, 
                "archetype_node_id": display_id, 
                "min": min_occ, 
                "max": max_occ,
                "aqlPath": aql_path, 
                "children": processed_children, 
                "is_leaf": False
            }
            if current_node_level != -1: 
                node_data['level'] = current_node_level
            if current_node_number_str: 
                node_data['section_number'] = current_node_number_str
            return node_data
        else:
            if processed_children: return processed_children

    if isinstance(current_node_json.get('children'), list):
        fallback_elements = []
        for child_json in current_node_json.get('children', []):
            processed_child = _process_node_for_ui(
                child_json, 
                lang_codes, 
                parent_aql_path, 
                current_node_number_str=current_node_number_str, 
                current_node_level=current_node_level 
            ) 
            if processed_child:
                if isinstance(processed_child, list): fallback_elements.extend(processed_child)
                else: fallback_elements.append(processed_child)
        if fallback_elements: return fallback_elements 
        
    return None 

def transform_web_template_to_questionnaire(web_template_data):
    if not web_template_data or not isinstance(web_template_data.get('tree'), dict):
        return {"_type": "COMPOSITION", "name": {"value": "Fout: Template 'tree' ongeldig"}, "version": "", "content": []}

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

    content_list_for_flask = []
    top_level_section_counter = 0

    for idx, top_level_json_node in enumerate(root_tree.get('children', [])):
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
                node_id = top_level_json_node.get('nodeId') or top_level_json_node.get('id', f'generated_section_id_{idx}')
                top_level_aql_path = f"/content[openEHR-EHR-SECTION.adhoc.v1 and name/value='{node_id}']"
                print(f"WARN: Hoofdsectie '{hoofdstuk_naam}' (index {idx}) heeft geen aqlPath. Een fallback is gegenereerd: {top_level_aql_path}")


            main_section_node = {
                "_type": top_level_rm_type,
                "name": {"value": hoofdstuk_naam},
                "archetype_node_id": top_level_json_node.get('nodeId') or top_level_json_node.get('id'),
                "aqlPath": top_level_aql_path,
                "min": top_level_json_node.get('min'),
                "max": top_level_json_node.get('max'),
                "children": [], # Wordt hieronder gevuld
                "is_leaf": False,
                "level": current_level_int,
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

                    processed_child = _process_node_for_ui(
                        child_json,
                        pref_langs,
                        parent_aql_path=top_level_aql_path,
                        current_node_number_str=child_full_number_str,
                        current_node_level=current_level_int + 1
                    )
                    if processed_child:
                        if isinstance(processed_child, list):
                            processed_children_list.extend(processed_child)
                        else:
                            processed_children_list.append(processed_child)
            
            main_section_node['children'] = processed_children_list
            content_list_for_flask.append(main_section_node)

        else:
            initial_level_for_other_types = 0 if top_level_rm_type in NUMBERABLE_CONTAINER_TYPES else -1
            processed_node = _process_node_for_ui(
                top_level_json_node, 
                pref_langs, 
                parent_aql_path=root_tree.get('aqlPath',''), 
                current_node_number_str="",
                current_node_level=initial_level_for_other_types 
            )
            if processed_node:
                if isinstance(processed_node, dict):
                    if 'level' not in processed_node and initial_level_for_other_types != -1 :
                        processed_node['level'] = initial_level_for_other_types
                    content_list_for_flask.append(processed_node)
                elif isinstance(processed_node, list): 
                    content_list_for_flask.extend(processed_node)
            
    return {
        "_type": "COMPOSITION", 
        "name": {"value": composition_name},
        "version": composition_version, 
        "archetype_node_id": composition_archetype_id,
        "content": content_list_for_flask
    }

def get_cached_questionnaire_structure():
    global _questionnaire_cache
    if _questionnaire_cache is None:
        print("INFO: Vragenlijst-cache is leeg. Bezig met laden en transformeren...")
        web_template_data = load_web_template_json(TEMPLATE_FILEPATH)
        if web_template_data:
            _questionnaire_cache = transform_web_template_to_questionnaire(web_template_data)
        else:
            _questionnaire_cache = {
                "_type": "COMPOSITION", 
                "name": {"value": "FOUT: Template kon niet geladen of verwerkt worden."}, 
                "version": "", 
                "content": []
            }
            flash("Kritieke fout: De vragenlijst kon niet worden opgebouwd.", "danger")
    return _questionnaire_cache

def _collect_element_paths(node_or_list):
    paths = set()
    if isinstance(node_or_list, list):
        for item in node_or_list:
            paths.update(_collect_element_paths(item))
        return paths

    if not isinstance(node_or_list, dict):
        return paths

    node = node_or_list
    if node.get('aqlPath') and (node.get('is_leaf') or isinstance(node.get('value'), dict) or 'children' in node):
        paths.add(node['aqlPath'])
    
    if isinstance(node.get('value'), dict) and node['value'].get('_type') == 'CHOICE':
        for option in node['value'].get('options', []):
            if isinstance(option, dict) and option.get('aqlPath'):
                paths.add(option['aqlPath'])

    if 'children' in node and isinstance(node['children'], list):
        for child in node['children']:
            paths.update(_collect_element_paths(child))
            
    return paths

@bp.route('/')
def index():
    questionnaire = get_cached_questionnaire_structure()
    if questionnaire and questionnaire.get('content'):
        return redirect(url_for('main.form_page', section_index=0))
    else:
        flash("Vragenlijst is leeg of kon niet geladen worden.", "warning")
        return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path={},
                               current_section=None, current_section_index=0, total_sections=0)


@bp.route('/formulier/<int:section_index>', methods=['GET', 'POST'])
def form_page(section_index):
    questionnaire = get_cached_questionnaire_structure()
    
    if not questionnaire or not questionnaire.get('content'):
        flash("Fout: Vragenlijststructuur is niet beschikbaar of leeg.", "danger")
        return render_template('index.html', title='Fout', questionnaire=None, comments_by_path={},
                               current_section=None, current_section_index=0, total_sections=0)

    total_sections = len(questionnaire.get('content', []))

    if not (0 <= section_index < total_sections):
        if total_sections == 0 and section_index == 0:
            flash("Er zijn geen secties beschikbaar in de vragenlijst.", "info")
            return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path={},
                                   current_section=None, current_section_index=0, total_sections=0)
        abort(404) 

    current_section_data = questionnaire['content'][section_index]

    comments_by_path = {}
    if current_section_data: 
        relevant_paths = _collect_element_paths(current_section_data)
        if relevant_paths:
            section_comments = Comment.query.filter(
                Comment.element_path.in_(list(relevant_paths))
            ).order_by(Comment.created_at.asc()).all()

            for comment_obj in section_comments:
                if comment_obj.element_path not in comments_by_path:
                    comments_by_path[comment_obj.element_path] = []
                comments_by_path[comment_obj.element_path].append(comment_obj)
        else: 
            print(f"INFO: Geen relevante AQL paden gevonden in sectie {section_index} voor commentaren.")
    else: 
        print("INFO: Geen huidige sectie data om commentaren voor op te halen.")

    return render_template('index.html', 
                           title=f"Review: {questionnaire.get('name',{}).get('value','Vragenlijst')} - Sectie {section_index + 1}", 
                           questionnaire=questionnaire, 
                           comments_by_path=comments_by_path,
                           current_section=current_section_data, 
                           current_section_index=section_index,
                           total_sections=total_sections
                          )

@bp.route('/formulier/commentaar', methods=['POST'])
def handle_comment_post():
    comment_text = request.form.get('comment_text')
    author_name = request.form.get('author_name', 'Anoniem')
    element_path = request.form.get('element_path')
    section_index_str = request.form.get('section_index')

    if not comment_text:
        flash('Commentaartekst mag niet leeg zijn.', 'warning')
    elif not element_path:
        flash('Fout: Element pad niet gevonden voor commentaar.', 'danger')
    elif section_index_str is None:
        flash('Fout: Sectie index niet meegegeven voor commentaar.', 'danger')
    else:
        try:
            section_index = int(section_index_str)

            amsterdam_tz = pytz.timezone('Europe/Amsterdam')
            timestamp = datetime.now(amsterdam_tz)

            comment = Comment(
                comment_text=comment_text, 
                author_name=author_name,
                element_path=element_path,
                created_at=timestamp
            )
            db.session.add(comment)
            db.session.commit()
            flash('Uw commentaar is succesvol geplaatst!', 'success')
            return redirect(url_for('main.form_page', section_index=section_index))
        except ValueError:
            flash('Fout: Ongeldige sectie index formaat.', 'danger')
        except Exception as e:
            db.session.rollback()
            flash(f'Er is een fout opgetreden bij het plaatsen van uw commentaar: {e}', 'danger')
    
    fallback_section_index = 0
    if section_index_str is not None:
        try:
            fallback_section_index = int(section_index_str)
        except ValueError:
            pass 
    return redirect(url_for('main.form_page', section_index=fallback_section_index))


# ====================================================================
# ========= WIJZIGING: Helper functie voor CSV Export aangepast =========
# ====================================================================
def _flatten_leaf_nodes_for_export(node, leaf_nodes_dict):
    """
    Doorloopt recursief de vragenlijst structuur en bouwt een platte dictionary
    met aqlPath als key en de node-data (naam en nodeId) als value,
    ALLEEN voor leaf nodes (vragen).
    """
    if isinstance(node, dict):
        # Sla de node op als deze een leaf is en een aqlPath heeft
        if node.get('is_leaf') and node.get('aqlPath'):
            leaf_nodes_dict[node['aqlPath']] = {
                'name': node.get('name', {}).get('value', 'Naamloos'),
                'nodeId': node.get('archetype_node_id', 'Geen ID') 
            }
        
        # Verwerk de kinderen van de node, ongeacht of de huidige node een leaf is
        # Dit is nodig omdat een container bladeren kan bevatten
        if 'children' in node and isinstance(node.get('children'), list):
            for child in node.get('children', []):
                _flatten_leaf_nodes_for_export(child, leaf_nodes_dict)
        
        # Verwerk ook 'options' binnen een 'CHOICE' type, aangezien dit ook leaves kunnen zijn
        if node.get('value') and isinstance(node['value'], dict) and node['value'].get('_type') == 'CHOICE':
            for option_node in node['value'].get('options', []):
                if isinstance(option_node, dict) and option_node.get('is_leaf') and option_node.get('aqlPath'):
                     leaf_nodes_dict[option_node['aqlPath']] = {
                        'name': option_node.get('name', {}).get('value', 'Naamloos Optie'),
                        'nodeId': option_node.get('archetype_node_id', 'Geen Optie ID')
                    }


    # Als de input een lijst is, verwerk elk item
    elif isinstance(node, list):
        for item in node:
            _flatten_leaf_nodes_for_export(item, leaf_nodes_dict)

@bp.route('/export/comments')
def export_comments_csv():
    """
    Genereert en serveert een CSV-bestand van alle commentaren voor alle vragen.
    """
    try:
        # 1. Haal de volledige vragenlijststructuur op
        questionnaire = get_cached_questionnaire_structure()
        all_questions_map = {}
        # Gebruik de nieuwe helper functie om ALLEEN leaf nodes (vragen) te verzamelen
        _flatten_leaf_nodes_for_export(questionnaire.get('content', []), all_questions_map)

        # 2. Haal alle commentaren op en groepeer ze per element_path
        all_comments_db = Comment.query.all()
        comments_for_csv = {}
        for comment_obj in all_comments_db:
            if comment_obj.element_path not in comments_for_csv:
                comments_for_csv[comment_obj.element_path] = []
            # Verwijder newlines uit commentaartekst voor CSV-compatibiliteit
            cleaned_comment_text = comment_obj.comment_text.replace('\r', '').replace('\n', ' ')
            comments_for_csv[comment_obj.element_path].append(cleaned_comment_text)

        # 3. Bouw het CSV-bestand in het geheugen
        output = io.StringIO()
        writer = csv.writer(output)

        # Schrijf de header
        writer.writerow(['Vraag', 'Node ID', 'Commentaar'])

        # ===== WIJZIGING: Itereer over alle vragen, niet alleen die met commentaar =====
        for path, question_info in all_questions_map.items():
            vraag = question_info['name']
            node_id = question_info['nodeId']
            
            # Haal commentaren op voor dit pad, of een lege lijst als er geen zijn
            current_comments_list = comments_for_csv.get(path, [])
            samengevoegd_commentaar = ", ".join(current_comments_list) # Gebruik komma als scheidingsteken
            
            writer.writerow([vraag, node_id, samengevoegd_commentaar])
        
        output.seek(0)

        # 4. Creëer en retourneer de Flask Response
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=commentaren_export.csv"}
        )

    except Exception as e:
        current_app.logger.error(f"Fout bij het genereren van CSV-export: {e}", exc_info=True)
        flash(f'Fout bij het genereren van CSV-export: {e}', 'danger')
        return redirect(request.referrer or url_for('main.index'))