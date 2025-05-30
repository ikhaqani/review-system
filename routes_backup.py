from flask import (
    render_template, flash, redirect, url_for, Blueprint,
    request, abort, current_app, Response, jsonify
)
from app import db
from app.models import Comment
import json
import os
from datetime import datetime
import pytz
import io
import csv
import re

bp = Blueprint('main', __name__)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_FILEPATH = os.path.join(BASE_DIR, 'templates_openehr', 'ACP-DUTCH.json')

ORIGINAL_SECTION_TYPES = {"SECTION"}
NUMBERABLE_CONTAINER_TYPES = {
    "EVALUATION", "ADMIN_ENTRY", "OBSERVATION",
    "INSTRUCTION", "ACTION", "GENERIC_ENTRY", "CLUSTER"
}
_questionnaire_cache = None
_original_web_template_cache = None

def get_original_web_template_data():
    global _original_web_template_cache
    if _original_web_template_cache is None:
        current_app.logger.info(f"DEBUG: _original_web_template_cache is None. Laden vanaf: {TEMPLATE_FILEPATH}")
        try:
            with open(TEMPLATE_FILEPATH, 'r', encoding='utf-8') as f:
                _original_web_template_cache = json.load(f)
            current_app.logger.info(f"Origineel Web Template '{TEMPLATE_FILEPATH}' succesvol geladen en gecached.")
        except FileNotFoundError:
            current_app.logger.error(f"FOUT: Origineel Web Template niet gevonden: {TEMPLATE_FILEPATH}")
            flash(f"Kritiek: Web Template '{os.path.basename(TEMPLATE_FILEPATH)}' niet gevonden.", "danger")
            _original_web_template_cache = None
        except json.JSONDecodeError as e:
            current_app.logger.error(f"FOUT: JSON parse error in {TEMPLATE_FILEPATH}: {e}")
            flash(f"Kritiek: Fout in Web Template JSON: {e}.", "danger")
            _original_web_template_cache = None
        except Exception as e:
            current_app.logger.error(f"FOUT: Onverwachte error bij laden {TEMPLATE_FILEPATH}: {e}", exc_info=True)
            flash(f"Kritiek: Onverwachte fout Web Template: {e}", "danger")
            _original_web_template_cache = None
    else:
        current_app.logger.info("DEBUG: Gebruik _original_web_template_cache uit cache.")
    return _original_web_template_cache

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
                    "_type": child_node_json.get('rmType'), "name": {"value": option_name},
                    "archetype_node_id": option_archetype_node_id, "aqlPath": option_aql_path,
                    "min": child_node_json.get('min'), "max": child_node_json.get('max'),
                    "value": option_value_data, "is_leaf": True
                })
            return {"_type": "CHOICE", "original_rm_type": node_rm_type, "options": choice_options, "value": None}
        effective_data_type = "DV_TEXT"

    value_structure = {}
    if effective_data_type in ['TEXT', 'STRING', 'DV_TEXT']: value_structure = {"_type": "DV_TEXT", "value": ""}
    elif effective_data_type in ['CODED_TEXT', 'DV_CODED_TEXT']:
        options = []
        for item in input_def.get('list', []):
            if isinstance(item, dict): options.append({"label": _get_node_name(item, lang_codes), "value": item.get('value')})
        value_structure = {"_type": "DV_CODED_TEXT", "value": "", "defining_code": {"code_string": "", "terminology_id": {"value": input_def.get('terminology', '')}}, "options": options}
    elif effective_data_type in ['BOOLEAN', 'DV_BOOLEAN']: value_structure = {"_type": "DV_BOOLEAN", "value": None}
    elif effective_data_type in ['INTEGER', 'COUNT', 'DV_COUNT']: value_structure = {"_type": "DV_COUNT", "magnitude": None}
    elif effective_data_type in ['DECIMAL', 'REAL', 'DOUBLE', 'DV_QUANTITY', 'QUANTITY']:
        units = input_def.get('units', '')
        validation_range = input_def.get('validation', {}).get('range')
        if not units and validation_range:
            if isinstance(validation_range, list) and validation_range: units = validation_range[0].get('units','')
            elif isinstance(validation_range, dict): units = validation_range.get('units', '')
        value_structure = {"_type": "DV_QUANTITY", "magnitude": None, "units": units}
    elif effective_data_type in ['DATETIME', 'DV_DATE_TIME']: value_structure = {"_type": "DV_DATE_TIME", "value": None}
    elif effective_data_type in ['DATE', 'DV_DATE']: value_structure = {"_type": "DV_DATE", "value": None}
    elif effective_data_type in ['TIME', 'DV_TIME']: value_structure = {"_type": "DV_TIME", "value": None}
    elif effective_data_type in ['IDENTIFIER', 'DV_IDENTIFIER']: value_structure = {"_type": "DV_IDENTIFIER", "id_value": "", "type": "", "issuer": "", "assigner": ""}
    elif effective_data_type in ['URI', 'DV_URI']: value_structure = {"_type": "DV_URI", "value": ""}
    elif effective_data_type.startswith("DV_INTERVAL"):
        inner_type_str = re.search(r"DV_INTERVAL<([A-Z_]+)>", effective_data_type).group(1) if re.search(r"DV_INTERVAL<([A-Z_]+)>", effective_data_type) else "DV_TEXT"
        lower_input_def, upper_input_def = (input_def, input_def)
        if isinstance(input_def_list, list):
            for sub_input in input_def_list:
                if sub_input.get("suffix") == "lower": lower_input_def = sub_input
                elif sub_input.get("suffix") == "upper": upper_input_def = sub_input
        value_structure = {
            "_type": "DV_INTERVAL",
            "lower": _create_value_structure({"rmType": inner_type_str, "inputs": [lower_input_def] if lower_input_def else []}, lang_codes),
            "upper": _create_value_structure({"rmType": inner_type_str, "inputs": [upper_input_def] if upper_input_def else []}, lang_codes),
            "lower_included": input_def.get("lower_included", True), "upper_included": input_def.get("upper_included", True)
        }
    elif effective_data_type == 'DV_DURATION': value_structure = {"_type": "DV_DURATION", "months": None, "weeks": None, "days": None, "hours": None, "minutes": None, "seconds": None}
    elif effective_data_type == 'DV_PROPORTION':
        prop_type = 0
        if input_def.get("list") and isinstance(input_def["list"], list) and input_def["list"]:
            try: prop_type = int(input_def["list"][0].get("value", 0))
            except ValueError: pass
        value_structure = {"_type": "DV_PROPORTION", "numerator": None, "denominator": None, "type": prop_type}
    else:
        current_app.logger.warning(f"Onbehandeld type '{effective_data_type}' voor node '{node_json.get('id', 'unknown')}' (rmType: {node_rm_type}).")
        return default_value_structure
    return value_structure

def _process_node_for_ui(current_node_json, lang_codes, parent_aql_path="", current_node_number_str="", current_node_level=-1):
    if not isinstance(current_node_json, dict): return None
    node_rm_type = current_node_json.get('rmType', '').upper()
    aql_path = current_node_json.get('aqlPath', '')
    relative_aql_path = aql_path.replace(parent_aql_path, '', 1).lstrip('/') if parent_aql_path else aql_path.lstrip('/')
    structural_metadata_to_skip = {'language', 'encoding', 'subject', 'category', 'territory', 'composer', 'health_care_facility', 'location'}
    if current_node_json.get('inContext') and relative_aql_path.count('/') == 0 and relative_aql_path in structural_metadata_to_skip:
        current_app.logger.info(f"Overslaan metadata node: {aql_path} (relatief: {relative_aql_path})")
        return None

    node_name = _get_node_name(current_node_json, lang_codes)
    display_id = current_node_json.get('nodeId') or current_node_json.get('id') or f'uid_{hash(aql_path):x}'[-6:]
    min_occ, max_occ = current_node_json.get('min'), current_node_json.get('max')

    is_leaf_element = (node_rm_type.startswith('DV_') and node_rm_type != 'DV_INTERVAL') or \
                      (node_rm_type == 'ELEMENT' and (
                          (not current_node_json.get('children') and current_node_json.get('inputs')) or
                          (current_node_json.get('children') and all(c.get('rmType','').upper().startswith('DV_') for c in current_node_json.get('children', []) if isinstance(c,dict))) or
                          (not current_node_json.get('children') and not current_node_json.get('inputs') and node_rm_type == 'ELEMENT') or
                          (current_node_json.get('children') and not any(c.get('rmType','').upper() in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}) for c in current_node_json.get('children',[]) if isinstance(c,dict)))
                      ))

    if is_leaf_element and aql_path:
        leaf_data = {
            "_type": node_rm_type, "name": {"value": node_name}, "archetype_node_id": display_id,
            "aqlPath": aql_path, "element_path_for_comments": aql_path,
            "min": min_occ, "max": max_occ,
            "value": _create_value_structure(current_node_json, lang_codes), "is_leaf": True
        }
        if current_node_level >= 0: leaf_data['level'] = current_node_level
        return leaf_data

    is_container = node_rm_type in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'})
    if is_container:
        processed_children = []
        child_counter = 0
        for child_json in current_node_json.get('children', []):
            if not isinstance(child_json, dict): continue
            child_level = current_node_level + 1 if current_node_level != -1 else 0
            child_num_str = ""
            if current_node_number_str and child_json.get('rmType','').upper() in (ORIGINAL_SECTION_TYPES | NUMBERABLE_CONTAINER_TYPES | {'EVENT_CONTEXT'}):
                child_counter += 1
                child_num_str = f"{current_node_number_str}.{child_counter}"
            
            processed_child = _process_node_for_ui(child_json, lang_codes, aql_path, child_num_str, child_level)
            if processed_child:
                if isinstance(processed_child, list): processed_children.extend(p for p in processed_child if p)
                else: processed_children.append(processed_child)
        
        if aql_path:
            node_data = {
                "_type": node_rm_type, "name": {"value": node_name}, "archetype_node_id": display_id,
                "min": min_occ, "max": max_occ, "aqlPath": aql_path,
                "children": processed_children, "is_leaf": False
            }
            if current_node_level != -1: node_data['level'] = current_node_level
            if current_node_number_str: node_data['section_number'] = current_node_number_str
            return node_data
        return processed_children if processed_children else None

    if isinstance(current_node_json.get('children'), list):
        fallback_elements = []
        for child_json in current_node_json.get('children',[]):
            processed_child = _process_node_for_ui(child_json, lang_codes, parent_aql_path, current_node_number_str, current_node_level)
            if processed_child:
                if isinstance(processed_child, list): fallback_elements.extend(p for p in processed_child if p)
                else: fallback_elements.append(processed_child)
        return fallback_elements if fallback_elements else None
    
    if aql_path and not node_rm_type.startswith("DV_"):
        current_app.logger.debug(f"Behandel node als leaf vanwege ontbrekende container/children eigenschappen: {aql_path}, type: {node_rm_type}")
        leaf_data = {
            "_type": node_rm_type if node_rm_type else "ELEMENT_FALLBACK", 
            "name": {"value": node_name}, "archetype_node_id": display_id,
            "aqlPath": aql_path, "element_path_for_comments": aql_path,
            "min": min_occ, "max": max_occ,
            "value": _create_value_structure(current_node_json, lang_codes), "is_leaf": True
        }
        if current_node_level >= 0: leaf_data['level'] = current_node_level
        return leaf_data
        
    current_app.logger.debug(f"Node niet verwerkt als leaf of container: {aql_path if aql_path else node_name}, type: {node_rm_type}")
    return None

def transform_web_template_to_questionnaire(web_template_data_raw):
    if not web_template_data_raw or not isinstance(web_template_data_raw.get('tree'), dict):
        return {"_type": "COMPOSITION", "name": {"value": "Fout: Template 'tree' ongeldig"}, "version": "", "content": []}

    pref_langs = [lang for lang in [web_template_data_raw.get('defaultLanguage', 'nl'), 'nl', 'en'] if lang]
    pref_langs = sorted(set(pref_langs), key=pref_langs.index) 

    root_tree = web_template_data_raw['tree']
    content_list = []
    if isinstance(root_tree.get('children'), list):
        for child_node_json in root_tree.get('children', []):
            processed = _process_node_for_ui(child_node_json, pref_langs, root_tree.get('aqlPath',''), current_node_level=0)
            if processed:
                if isinstance(processed, list): content_list.extend(p for p in processed if p)
                else: content_list.append(processed)
    
    return {
        "_type": "COMPOSITION",
        "name": {"value": _get_node_name(root_tree, pref_langs)},
        "version": web_template_data_raw.get('version', web_template_data_raw.get('semVer', '')),
        "archetype_node_id": root_tree.get('nodeId', root_tree.get('id', 'root_id_onbekend')),
        "content": content_list
    }

def get_cached_questionnaire_structure():
    global _questionnaire_cache
    if _questionnaire_cache is None:
        current_app.logger.info("DEBUG: _questionnaire_cache is None. Laden...")
        raw_web_template_data = get_original_web_template_data()
        if raw_web_template_data:
            _questionnaire_cache = transform_web_template_to_questionnaire(raw_web_template_data)
            current_app.logger.info(f"DEBUG: _questionnaire_cache.content lengte: {len(_questionnaire_cache.get('content', []))}")
            if not _questionnaire_cache.get("content"):
                 current_app.logger.warning("Getransformeerde vragenlijst heeft lege 'content'.")
        else:
            _questionnaire_cache = {"_type": "COMPOSITION", "name": {"value": "FOUT: Template niet geladen"}, "version": "", "content": []}
            current_app.logger.warning("DEBUG: _questionnaire_cache gezet naar FOUT vanwege ontbrekende raw_web_template_data.")
    else:
        current_app.logger.info("DEBUG: Gebruik _questionnaire_cache uit cache.")
    return _questionnaire_cache

def _collect_element_paths(node_or_list):
    paths = set()
    if isinstance(node_or_list, list):
        for item in node_or_list: paths.update(_collect_element_paths(item))
    elif isinstance(node_or_list, dict):
        node = node_or_list
        if node.get('aqlPath'): paths.add(node['aqlPath'])
        if isinstance(node.get('value'), dict) and node['value'].get('_type') == 'CHOICE':
            for option in node['value'].get('options', []):
                if isinstance(option, dict) and option.get('aqlPath'): paths.add(option['aqlPath'])
        if 'children' in node and isinstance(node['children'], list):
            for child in node['children']: paths.update(_collect_element_paths(child))
    return paths

@bp.route('/')
def index():
    return redirect(url_for('main.form_page'))

@bp.route('/formulier', methods=['GET'])
def form_page():
    questionnaire_for_js_dict = get_cached_questionnaire_structure()
    original_web_template_dict_for_mb = get_original_web_template_data()

    current_app.logger.info(f"DEBUG form_page: Type original_web_template_dict_for_mb = {type(original_web_template_dict_for_mb)}")
    current_app.logger.info(f"DEBUG form_page: original_web_template_dict_for_mb is None? {original_web_template_dict_for_mb is None}")
    current_app.logger.info(f"DEBUG form_page: Type questionnaire_for_js_dict = {type(questionnaire_for_js_dict)}")
    current_app.logger.info(f"DEBUG form_page: questionnaire_for_js_dict is None? {questionnaire_for_js_dict is None}")
    if questionnaire_for_js_dict:
        current_app.logger.info(f"DEBUG form_page: questionnaire_for_js_dict.get('content') is None or empty? {not questionnaire_for_js_dict.get('content')}")

    error_msg_for_template = None
    if not original_web_template_dict_for_mb:
        error_msg_for_template = "Kritieke fout: De web template definitie (ACP-DUTCH.json) kon niet geladen worden. Formulier kan niet getoond worden."
    elif not questionnaire_for_js_dict: 
         current_app.logger.warning("Fout: De interne vragenlijststructuur (voor commentaren) kon niet worden opgebouwd, maar ruwe template is wel geladen.")

    all_comments = Comment.query.order_by(Comment.created_at.asc()).all()
    comments_by_path_for_js_dict = {}
    for comment in all_comments:
        comments_by_path_for_js_dict.setdefault(comment.element_path, []).append({
            "author_name": comment.author_name,
            "comment_text": comment.comment_text,
            "created_at": comment.created_at.isoformat() if comment.created_at else None
        })
    
    return render_template('index.html',
                           title=f"Review: {questionnaire_for_js_dict.get('name',{}).get('value','Vragenlijst') if questionnaire_for_js_dict else 'Vragenlijst'}",
                           questionnaire_for_js=questionnaire_for_js_dict,
                           comments_by_path_for_js=comments_by_path_for_js_dict,
                           web_template_for_mb_js=original_web_template_dict_for_mb,
                           error_message=error_msg_for_template)

def flash_messages_contain_critical_template_error(messages):
    if not messages: return False
    for category, message in messages:
        if category == 'danger' and "Kritiek:" in message: return True
    return False

@bp.route('/submit-openehr-data', methods=['POST'])
def submit_openehr_data():
    if not request.is_json:
        return jsonify({"error": "Request moet JSON zijn", "status": "error"}), 400
    data = request.get_json()
    current_app.logger.info(f"Ontvangen Medblocks UI data: {json.dumps(data, indent=2, ensure_ascii=False)}")
    return jsonify({"message": "Data succesvol ontvangen (simulatie)", "status": "success"}), 200

@bp.route('/formulier/commentaar', methods=['POST'])
def handle_comment_post():
    comment_text = request.form.get('comment_text')
    author_name = request.form.get('author_name', 'Anoniem')
    element_path = request.form.get('element_path')

    if not comment_text or not comment_text.strip(): flash('Commentaartekst mag niet leeg zijn.', 'warning')
    elif not element_path: flash('Fout: Element pad niet gevonden voor commentaar.', 'danger')
    else:
        try:
            comment = Comment(
                comment_text=comment_text, author_name=author_name,
                element_path=element_path, created_at=datetime.now(pytz.timezone('Europe/Amsterdam'))
            )
            db.session.add(comment)
            db.session.commit()
            flash('Uw opmerking is succesvol geplaatst!', 'success')
            safe_element_path_for_anchor = re.sub(r'[^a-zA-Z0-9\-_]', '-', element_path)
            anker_id = f"comment-container-{safe_element_path_for_anchor}"
            return redirect(url_for('main.form_page', _anchor=anker_id))
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Fout bij plaatsen commentaar: {e}", exc_info=True)
            flash('Er is een fout opgetreden bij het plaatsen van uw opmerking.', 'danger')
    return redirect(url_for('main.form_page'))

def _flatten_leaf_nodes_for_export(node, leaf_nodes_dict):
    if isinstance(node, dict):
        if node.get('is_leaf') and node.get('aqlPath'):
            parent_aql_path = node['aqlPath']
            parent_name = node.get('name', {}).get('value', 'Naamloos')
            parent_node_id = node.get('archetype_node_id', 'Geen ID')
            if node.get('value') and isinstance(node['value'], dict) and node['value'].get('_type') == 'CHOICE':
                options = node['value'].get('options', [])
                if not options: leaf_nodes_dict[parent_aql_path] = {'csv_name': parent_name, 'csv_node_id': parent_node_id, 'comment_path': parent_aql_path}
                else:
                    for i, opt in enumerate(options):
                        if isinstance(opt, dict):
                            opt_id = opt.get('archetype_node_id', '')
                            name = f"{parent_name} ({opt_id})" if i > 0 and opt_id and opt_id.lower() != 'value' else (f"{parent_name} (als {opt.get('value',{}).get('_type',f'Optie{i+1}')})" if i > 0 else parent_name)
                            leaf_nodes_dict[f"{parent_aql_path}#CHOICE_OPT_{i}_{opt_id}"] = {'csv_name': name, 'csv_node_id': parent_node_id, 'comment_path': parent_aql_path}
            else: leaf_nodes_dict[parent_aql_path] = {'csv_name': parent_name, 'csv_node_id': parent_node_id, 'comment_path': parent_aql_path}
        
        if 'children' in node and isinstance(node.get('children'), list):
            for child in node.get('children', []): 
                _flatten_leaf_nodes_for_export(child, leaf_nodes_dict)
    elif isinstance(node, list):
        for item in node:
            _flatten_leaf_nodes_for_export(item, leaf_nodes_dict)

@bp.route('/export/comments')
def export_comments_csv():
    try:
        questionnaire = get_cached_questionnaire_structure()
        all_questions_map = {}
        _flatten_leaf_nodes_for_export(questionnaire.get('content', []), all_questions_map)
        
        all_comments_db = Comment.query.all()
        comments_for_csv = {} 
        for comment_obj in all_comments_db:
            comments_for_csv.setdefault(comment_obj.element_path, []).append(
                comment_obj.comment_text.replace('\r', '').replace('\n', ' ')
            )

        output = io.StringIO()
        writer = csv.writer(output, quotechar='"', quoting=csv.QUOTE_MINIMAL)
        writer.writerow(['Vraag', 'Node ID', 'Commentaar']) 

        for unique_key, question_data in all_questions_map.items():
            vraag_display_name = question_data['csv_name']
            node_id_at_code = question_data['csv_node_id']
            comment_lookup_path = question_data['comment_path']
            
            current_comments_list = comments_for_csv.get(comment_lookup_path, [])
            samengevoegd_commentaar = ", ".join(current_comments_list)
            
            writer.writerow([vraag_display_name, node_id_at_code, samengevoegd_commentaar])
        
        output.seek(0)
        return Response(
            output,
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment;filename=commentaren_export.csv"}
        )
    except Exception as e:
        current_app.logger.error(f"Fout bij het genereren van CSV-export: {e}", exc_info=True)
        flash('Fout bij het genereren van CSV-export.', 'danger')
        return redirect(request.referrer or url_for('main.index'))

