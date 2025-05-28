# app/main/routes.py

from flask import render_template, flash, redirect, url_for, Blueprint, request, abort, current_app
from app import db # Ervan uitgaande dat 'app' uw Flask app instance is en 'db' uw SQLAlchemy instance
from app.models import Comment # Zorg dat dit pad correct is naar uw Comment model
import json
import os
# Voor caching, als je een complexere cache-strategie wilt, overweeg Flask-Caching
# import threading # Nodig voor thread-safe simpele cache als je geen Flask-Caching gebruikt

bp = Blueprint('main', __name__)

# Pad naar het JSON template bestand (kan later via app.config als je wilt)
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_FILEPATH = os.path.join(BASE_DIR, 'templates_openehr', 'ACP-DUTCH.json')


# Definieer de rmTypes per niveau voor de nummering
SECTION_TYPES = {"SECTION"}
# SUBSECTION_TYPES omvat nu ook CLUSTER voor consistente nummering onder secties
SUBSECTION_AND_CLUSTER_TYPES = {"EVALUATION", "ADMIN_ENTRY", "OBSERVATION", "INSTRUCTION", "ACTION", "GENERIC_ENTRY", "CLUSTER"}
# CLUSTER_TYPES apart als je specifieke CLUSTER logica nodig hebt die verschilt van andere SUBSECTION_TYPES
CLUSTER_TYPES = {"CLUSTER"} 

# Globale variabele voor simpele caching van de getransformeerde template
# Voor productie-apps met meerdere workers/threads, overweeg een robuustere cache zoals Flask-Caching of Redis.
_questionnaire_cache = None
# _cache_lock = threading.Lock() # Als je een simpele lock-gebaseerde cache wilt voor multi-threaded dev server


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
    if not isinstance(node_json, dict): return "Ongeldige Node Structuur" # Iets duidelijker dan "Ongeldige Node"
    
    # 1. Directe 'localizedName' (vaak gebruikt in templates)
    if isinstance(node_json.get('localizedName'), str) and node_json['localizedName'].strip():
        return node_json['localizedName']
    
    # 2. Directe 'name' als er geen 'localizedNames' dictionary is (snelle check)
    if isinstance(node_json.get('name'), str) and not node_json.get('localizedNames') and node_json['name'].strip():
        return node_json['name']
        
    # 3. 'localizedNames' dictionary met voorkeurstalen
    localized_names_dict = node_json.get('localizedNames', {})
    if isinstance(localized_names_dict, dict):
        for lang_code in lang_codes:
            if lang_code in localized_names_dict and \
               isinstance(localized_names_dict[lang_code], str) and \
               localized_names_dict[lang_code].strip():
                return localized_names_dict[lang_code]

    # 4. Fallback naar 'name' als die nog niet geprobeerd is en bestaat
    if isinstance(node_json.get('name'), str) and node_json['name'].strip():
        return node_json['name']
        
    # 5. Fallback naar 'label' (vaak gebruikt in DV_CODED_TEXT opties)
    if isinstance(node_json.get('label'), str) and node_json['label'].strip():
        return node_json['label']

    # 6. Fallback naar 'id' als laatste redmiddel voor een naam
    node_id = node_json.get('id')
    if isinstance(node_id, str) and node_id.strip():
        return node_id # Geef de ID terug als naam

    return "Naamloos Veld" # Alleen als echt niets gevonden is

def _create_value_structure(node_json, lang_codes=['nl', 'en']):
    default_value_structure = {"_type": "DV_TEXT", "value": f"[Onbekend Type: {node_json.get('rmType','GEEN RM TYPE')}]"}
    if not isinstance(node_json, dict): return default_value_structure

    node_rm_type = node_json.get('rmType', '').upper()
    input_def_list = node_json.get('inputs', [])
    input_def = input_def_list[0] if isinstance(input_def_list, list) and input_def_list else {}
    
    # Bepaal het effectieve datatype. Als node_rm_type al een DV_ type is, gebruik dat. Anders, kijk naar inputs.
    effective_data_type = node_rm_type if node_rm_type.startswith("DV_") else input_def.get('type', '').upper()

    # Speciale behandeling voor ELEMENT nodes die een CHOICE van datatypes bevatten (via hun kinderen)
    if node_rm_type == 'ELEMENT' and not effective_data_type.startswith("DV_"):
        node_children = node_json.get('children', [])
        if node_children and all(isinstance(child, dict) for child in node_children):
            choice_options = []
            for child_node_json in node_children:
                # Elk kind van dit ELEMENT is een optie in de CHOICE.
                # We maken een representatie van elke optie.
                option_name = _get_node_name(child_node_json, lang_codes)
                option_archetype_node_id = child_node_json.get('id') or child_node_json.get('nodeId') # Soms 'id', soms 'nodeId' voor archetypische ID
                option_aql_path = child_node_json.get('aqlPath', '')
                
                # De 'value' van de optie is de datastructuur van het kind-datatype zelf.
                # Dit zorgt ervoor dat de UI weet hoe het invulveld voor deze specifieke keuze gerenderd moet worden.
                option_value_data = _create_value_structure(child_node_json, lang_codes)

                choice_options.append({
                    "_type": child_node_json.get('rmType'), 
                    "name": {"value": option_name},
                    "archetype_node_id": option_archetype_node_id,
                    "aqlPath": option_aql_path, 
                    "min": child_node_json.get('min'),
                    "max": child_node_json.get('max'),
                    "value": option_value_data, # De daadwerkelijke invulstructuur voor deze optie
                    "is_leaf": True # Elke optie binnen de choice is een renderbaar leaf-element
                })
            return {
                "_type": "CHOICE", 
                "original_rm_type": node_rm_type, # Behoud het originele RM type van de container
                "options": choice_options,
                "value": None # Geselecteerde waarde van de choice (initieel geen)
            }
        # Als een ELEMENT geen DV-input heeft en geen kinderen, fallback naar DV_TEXT
        effective_data_type = "DV_TEXT"

    value_structure = {}
    if effective_data_type in ['TEXT', 'STRING', 'DV_TEXT']:
        value_structure = {"_type": "DV_TEXT", "value": ""}
    elif effective_data_type in ['CODED_TEXT', 'DV_CODED_TEXT']:
        options = []
        options_list_source = input_def.get('list', [])
        for option_item in options_list_source:
            if isinstance(option_item, dict):
                label = _get_node_name(option_item, lang_codes) # Gebruikt ook localizedLabels etc.
                options.append({"label": label, "value": option_item.get('value')})
        value_structure = {
            "_type": "DV_CODED_TEXT", "value": "", 
            "defining_code": {"code_string": "", "terminology_id": {"value": input_def.get('terminology', '')}},
            "options": options
        }
    elif effective_data_type in ['BOOLEAN', 'DV_BOOLEAN']:
        value_structure = {"_type": "DV_BOOLEAN", "value": None} # Gebruik None voor niet-geselecteerd
    elif effective_data_type in ['INTEGER', 'COUNT', 'DV_COUNT']:
        value_structure = {"_type": "DV_COUNT", "magnitude": None}
    elif effective_data_type in ['DECIMAL', 'REAL', 'DOUBLE', 'DV_QUANTITY', 'QUANTITY']:
        units = input_def.get('units', '')
        validation_range = input_def.get('validation', {}).get('range', {})
        if not units and isinstance(validation_range.get('units'), str) : units = validation_range['units']
        value_structure = {"_type": "DV_QUANTITY", "magnitude": None, "units": units}
    elif effective_data_type in ['DATETIME', 'DV_DATE_TIME']:
        value_structure = {"_type": "DV_DATE_TIME", "value": None} # Placeholder voor datum-tijd
    elif effective_data_type in ['DATE', 'DV_DATE']:
        value_structure = {"_type": "DV_DATE", "value": None} # Placeholder voor datum
    elif effective_data_type in ['TIME', 'DV_TIME']:
        value_structure = {"_type": "DV_TIME", "value": None} # Placeholder voor tijd
    elif effective_data_type in ['IDENTIFIER', 'DV_IDENTIFIER']:
        # Standaard structuur voor DV_IDENTIFIER, velden kunnen leeg zijn
        value_structure = {"_type": "DV_IDENTIFIER", "id_value": "", "type": "", "issuer": "", "assigner": ""}
    elif effective_data_type in ['URI', 'DV_URI']:
        value_structure = {"_type": "DV_URI", "value": ""}
    elif effective_data_type.startswith('DV_INTERVAL') or effective_data_type in ['DV_DURATION', 'DV_PROPORTION']:
        # Generieke placeholder voor complexere DV types die verdere specificatie in de UI vereisen
        value_structure = {"_type": effective_data_type, "value": f"[{effective_data_type.replace('DV_', '')} placeholder]"}
    else:
        # Fallback als het type niet specifiek behandeld wordt
        print(f"WARN: Onbehandeld effective_data_type '{effective_data_type}' voor node '{node_json.get('id', 'unknown id')}' (rmType: {node_rm_type}). Gebruik default structuur.")
        return default_value_structure
        
    return value_structure


def _process_node_for_ui(current_node_json, lang_codes=['nl', 'en'], parent_aql_path="", current_node_full_number_as_parent="", current_node_level=0):
    if not isinstance(current_node_json, dict): return None
    
    node_id_val = current_node_json.get('id')
    node_rm_type = current_node_json.get('rmType', '').upper()
    aql_path = current_node_json.get('aqlPath', '')

    # Filter metadata nodes (zoals /language, /encoding direct onder een item)
    relative_aql_path = aql_path.replace(parent_aql_path, '', 1).lstrip('/') if parent_aql_path else aql_path.lstrip('/')
    is_structural_metadata = current_node_json.get('inContext') is True and \
                             (relative_aql_path.count('/') == 0 and \
                              relative_aql_path in ['language', 'encoding', 'subject', 'category', 'territory', 'composer', 'setting', 'start_time'])
    if is_structural_metadata:
        return None

    node_name = _get_node_name(current_node_json, lang_codes)
    display_id = node_id_val if node_id_val else current_node_json.get('nodeId', 'uid_' + str(hash(aql_path))[-6:])
    min_occ = current_node_json.get('min')
    max_occ = current_node_json.get('max')

    # Bepaal of het een leaf element is.
    # Een ELEMENT is een leaf als het geen structurele kinderen heeft (SECTION, CLUSTER, etc.)
    # of als het kinderen heeft die allemaal DV_ types zijn (wat het een soort CHOICE van data values maakt).
    is_leaf_element = False
    if node_rm_type.startswith('DV_') and node_rm_type not in ['DV_INTERVAL']: # DV_INTERVAL kan kinderen hebben voor upper/lower
        is_leaf_element = True
    elif node_rm_type == 'ELEMENT':
        children_nodes = current_node_json.get('children', [])
        if not children_nodes and not current_node_json.get('inputs'): # Geen kinderen en geen inputs -> geen echte leaf
             is_leaf_element = False
        elif children_nodes and all(child.get('rmType','').upper().startswith('DV_') or \
                                    child.get('rmType','').upper().startswith('DV_INTERVAL') \
                                    for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True # Wordt een CHOICE met DV_ opties, behandeld als leaf in UI structuur
        elif not any(child.get('rmType','').upper() in (SECTION_TYPES | SUBSECTION_AND_CLUSTER_TYPES) \
                     for child in children_nodes if isinstance(child,dict)):
            is_leaf_element = True # Geen structurele kinderen, dus leaf
    
    if is_leaf_element and aql_path : # aql_path is nodig om het uniek te identificeren
        return {
            "_type": node_rm_type, 
            "name": {"value": node_name}, 
            "archetype_node_id": display_id,
            "aqlPath": aql_path, 
            "element_path_for_comments": aql_path, # Gebruikt voor koppelen van commentaar
            "min": min_occ, 
            "max": max_occ,
            "value": _create_value_structure(current_node_json, lang_codes),
            "is_leaf": True
        }

    # Bepaal of het een container is die getoond moet worden (Sectie, Subsection, Cluster)
    is_displayable_container = node_rm_type in SECTION_TYPES or \
                               node_rm_type in SUBSECTION_AND_CLUSTER_TYPES
    
    if is_displayable_container:
        processed_children = []
        json_children_list = current_node_json.get('children', [])
        
        child_numbering_counter = 0 # Gebruik één teller voor alle genummerde kinderen binnen deze container

        if isinstance(json_children_list, list):
            for child_idx, child_json_node in enumerate(json_children_list):
                if not isinstance(child_json_node, dict): continue

                child_rm_type = child_json_node.get('rmType', '').upper()
                child_full_number = ""
                
                # Niveau voor het kind bepalen. Level 0 is gereserveerd voor de compositie root.
                # Top-level SECTIONS krijgen level 1. Hun kinderen (subsections/clusters) level 2, etc.
                child_level = current_node_level + 1 
                
                # Nummering alleen toepassen als de huidige node genummerd is (een section_number heeft)
                # en het kind een type is dat genummerd moet worden.
                if current_node_full_number_as_parent and \
                   child_rm_type in SUBSECTION_AND_CLUSTER_TYPES: # Alleen geneste subsections/clusters nummeren
                    child_numbering_counter += 1
                    child_full_number = f"{current_node_full_number_as_parent}.{child_numbering_counter}"
                
                processed_child_node = _process_node_for_ui(
                    child_json_node, 
                    lang_codes, 
                    aql_path, # parent_aql_path voor het kind is de aql_path van de huidige node
                    current_node_full_number_as_parent=child_full_number if child_full_number else current_node_full_number_as_parent, # Geef nummer door
                    current_node_level=child_level
                )
                
                if processed_child_node:
                    # Als _process_node_for_ui een lijst teruggeeft (bv. voor een niet-displayable container), voeg elementen toe
                    if isinstance(processed_child_node, list):
                        for sub_child in processed_child_node: # Verwerk elk item in de lijst
                            if isinstance(sub_child, dict):
                                if child_level > 0 and 'level' not in sub_child: sub_child['level'] = child_level
                                # Nummering voor deze sub_child is al afgehandeld in de recursieve call
                            processed_children.append(sub_child)
                    elif isinstance(processed_child_node, dict):
                        if child_level > 0 : processed_child_node['level'] = child_level # Niveau toevoegen
                        if child_full_number: processed_child_node['section_number'] = child_full_number # Nummer toevoegen
                        processed_children.append(processed_child_node)
        
        # Een container wordt alleen geretourneerd als het kinderen heeft, 
        # of als het een "echte" container is (geen ELEMENT dat toevallig geen leaf was)
        # en een naam + aqlPath heeft.
        if processed_children or (node_name != "Naamloos Veld" and aql_path and node_rm_type != 'ELEMENT'):
            return {
                "_type": node_rm_type, 
                "name": {"value": node_name},
                "archetype_node_id": display_id, 
                "min": min_occ, 
                "max": max_occ,
                "aqlPath": aql_path, 
                "children": processed_children, 
                "is_leaf": False
            }

    # Fallback: als het geen leaf is en geen displayable container, maar wel kinderen heeft,
    # verwerk dan de kinderen en geef ze als een platte lijst terug.
    # Dit gebeurt bijv. voor EVENT_CONTEXT of andere niet-renderbare tussenliggende nodes.
    if isinstance(current_node_json.get('children'), list):
        fallback_elements = []
        for child_json in current_node_json.get('children', []):
            processed_child = _process_node_for_ui(
                child_json, 
                lang_codes, 
                aql_path, # parent_aql_path voor het kind
                current_node_full_number_as_parent=current_node_full_number_as_parent, # Geef nummer door
                current_node_level=current_node_level # Niveau blijft gelijk voor "platte" lijst
            ) 
            if processed_child:
                if isinstance(processed_child, list): fallback_elements.extend(processed_child)
                else: fallback_elements.append(processed_child)
        if fallback_elements: return fallback_elements
        
    return None # Geen renderbare output voor deze node


def transform_web_template_to_questionnaire(web_template_data):
    if not web_template_data or not isinstance(web_template_data.get('tree'), dict):
        return {"_type": "COMPOSITION", "name": {"value": "Fout: Template 'tree' ongeldig"}, "version": "", "content": []}

    default_lang = web_template_data.get('defaultLanguage', 'nl')
    available_langs = web_template_data.get('languages', ['nl', 'en'])
    # Zorg dat 'nl' vooraan staat als het beschikbaar is, daarna de default, dan de rest
    pref_langs = []
    if 'nl' in available_langs: pref_langs.append('nl')
    if default_lang not in pref_langs and default_lang in available_langs: pref_langs.append(default_lang)
    for lang in available_langs:
        if lang not in pref_langs: pref_langs.append(lang)
    if not pref_langs: pref_langs = ['nl', 'en'] # Fallback als alles leeg is


    root_tree = web_template_data['tree']
    composition_name = _get_node_name(root_tree, pref_langs)
    composition_version = web_template_data.get('version', web_template_data.get('semVer', ''))
    composition_archetype_id = root_tree.get('nodeId', root_tree.get('id', 'root_id_onbekend'))

    content_list_for_flask = []
    top_level_section_counter = 0

    for top_level_json_node in root_tree.get('children', []):
        if not isinstance(top_level_json_node, dict): continue
        
        # Optioneel: filter EVENT_CONTEXT hier als het niet als een sectie getoond moet worden
        # if top_level_json_node.get('rmType', '').upper() == 'EVENT_CONTEXT':
        #     print(f"INFO: Overslaan van top-level EVENT_CONTEXT node: {_get_node_name(top_level_json_node, pref_langs)}")
        #     continue

        actual_node_number_for_this_top_node = ""
        node_level_for_this_top_node = 0 
        top_level_rm_type = top_level_json_node.get('rmType', '').upper()
        
        if top_level_rm_type in SECTION_TYPES:
            top_level_section_counter += 1
            actual_node_number_for_this_top_node = str(top_level_section_counter)
            node_level_for_this_top_node = 1 # Hoofdsecties zijn level 1
        
        processed_node = _process_node_for_ui(
            top_level_json_node, 
            pref_langs, 
            parent_aql_path=root_tree.get('aqlPath',''), # AQL path van de COMPOSITION root is leeg
            current_node_full_number_as_parent=actual_node_number_for_this_top_node, 
            current_node_level=node_level_for_this_top_node # Level 0 is de root, level 1 voor eerste secties
        )
        
        if processed_node:
            if isinstance(processed_node, dict):
                # Zorg dat level en section_number consistent worden toegevoegd
                if node_level_for_this_top_node > 0: # Alleen level toevoegen als het een genummerde hoofdsectie is
                    processed_node['level'] = node_level_for_this_top_node
                if actual_node_number_for_this_top_node: # Voeg nummer toe als het een genummerde hoofdsectie is
                    processed_node['section_number'] = actual_node_number_for_this_top_node
                content_list_for_flask.append(processed_node)
            elif isinstance(processed_node, list): 
                # Dit gebeurt als _process_node_for_ui een lijst van kinderen teruggeeft 
                # (bv. voor een niet-displayable container zoals EVENT_CONTEXT)
                # We groeperen deze onder een tijdelijke node voor weergave.
                temp_group_node = {
                    "_type": top_level_json_node.get('rmType','GROUP').upper(), # Gebruik rmType of fallback naar GROUP
                    "name": {"value": _get_node_name(top_level_json_node, pref_langs)},
                    "archetype_node_id": top_level_json_node.get('id', f"unknown_group_{top_level_json_node.get('name','unnamed')}"),
                    "min": top_level_json_node.get('min'), 
                    "max": top_level_json_node.get('max'),
                    "children": processed_node, 
                    "is_leaf": False,
                    "aqlPath": top_level_json_node.get('aqlPath','') # Behoud AQL pad van de originele node
                }
                # Als deze groep genummerd moet worden (onwaarschijnlijk voor EVENT_CONTEXT, maar voor de zekerheid)
                if node_level_for_this_top_node > 0:
                    temp_group_node['level'] = node_level_for_this_top_node
                if actual_node_number_for_this_top_node:
                    temp_group_node['section_number'] = actual_node_number_for_this_top_node
                content_list_for_flask.append(temp_group_node)
            
    return {
        "_type": "COMPOSITION", 
        "name": {"value": composition_name},
        "version": composition_version, 
        "archetype_node_id": composition_archetype_id,
        "content": content_list_for_flask
    }

def get_cached_questionnaire_structure():
    """
    Haalt de vragenlijststructuur op uit de cache. 
    Als de cache leeg is, laadt en transformeert het de template.
    """
    global _questionnaire_cache
    # Optioneel: lock voor thread safety als je geen Flask-Caching gebruikt in een multi-thread omgeving
    # with _cache_lock:
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
    """
    Recursieve functie om alle unieke 'aqlPath' waarden van leaf-nodes 
    (of nodes met een 'value' structuur) te verzamelen binnen een gegeven node of lijst van nodes.
    """
    paths = set()
    if isinstance(node_or_list, list):
        for item in node_or_list:
            paths.update(_collect_element_paths(item))
        return paths

    if not isinstance(node_or_list, dict):
        return paths

    node = node_or_list
    # Een element wordt als relevant beschouwd als het een 'aqlPath' heeft en ofwel een leaf is,
    # of een 'value' dict heeft (wat CHOICE types ook hebben).
    if node.get('aqlPath') and (node.get('is_leaf') or isinstance(node.get('value'), dict)):
        paths.add(node['aqlPath'])
    
    # Als het een CHOICE type is, verzamel paden van zijn opties
    if isinstance(node.get('value'), dict) and node['value'].get('_type') == 'CHOICE':
        for option in node['value'].get('options', []):
            if isinstance(option, dict) and option.get('aqlPath'):
                 paths.add(option['aqlPath']) # Voeg paden van de keuzes toe

    if 'children' in node and isinstance(node['children'], list):
        for child in node['children']:
            paths.update(_collect_element_paths(child))
            
    return paths

@bp.route('/')
def index():
    return redirect(url_for('main.form_page', section_index=0))

@bp.route('/formulier/<int:section_index>', methods=['GET', 'POST'])
def form_page(section_index):
    questionnaire = get_cached_questionnaire_structure()
    
    if not questionnaire or not questionnaire.get('content'):
        flash("Fout: Vragenlijststructuur is niet beschikbaar of leeg.", "danger")
        # Geef een fallback template weer of redirect naar een error pagina
        return render_template('error_page.html', message="Vragenlijst kon niet geladen worden.")

    total_sections = len(questionnaire.get('content', []))

    if not (0 <= section_index < total_sections):
        # Als er geen secties zijn, en index is 0, kan dit ook een 404 geven.
        # Misschien een vriendelijkere pagina als total_sections == 0?
        if total_sections == 0 and section_index == 0:
             flash("Er zijn geen secties beschikbaar in de vragenlijst.", "info")
             # Toon een lege pagina of een specifieke melding
             return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path={},
                                   current_section=None, current_section_index=0, total_sections=0)
        abort(404)

    current_section_data = questionnaire['content'][section_index]

    if request.method == 'POST':
        comment_text = request.form.get('comment_text')
        author_name = request.form.get('author_name') # Zorg dat 'author_name' in je formulier zit
        element_path = request.form.get('element_path')
        
        # Basisvalidatie
        if not author_name:
             flash('Naam van de auteur is verplicht voor commentaar.', 'warning')
        elif not comment_text:
            flash('Commentaartekst mag niet leeg zijn.', 'warning')
        elif not element_path:
            flash('Fout: Element pad niet gevonden voor commentaar.', 'danger') # Zou niet mogen gebeuren
        else:
            comment = Comment(
                comment_text=comment_text, 
                author_name=author_name,
                element_path=element_path
            )
            db.session.add(comment)
            try:
                db.session.commit()
                flash('Uw commentaar is succesvol geplaatst!', 'success')
            except Exception as e:
                db.session.rollback()
                flash(f'Er is een fout opgetreden bij het plaatsen van uw commentaar: {e}', 'danger')
            return redirect(url_for('main.form_page', section_index=section_index))

    # Optimalisatie: Haal alleen commentaren op voor de elementen in de huidige sectie
    comments_by_path = {}
    if current_section_data: # Alleen als er een huidige sectie is
        relevant_paths = _collect_element_paths(current_section_data)
        if relevant_paths:
            section_comments = Comment.query.filter(
                Comment.element_path.in_(list(relevant_paths)) # SQLAlchemy .in_() verwacht een lijst of tuple
            ).order_by(Comment.created_at.asc()).all()

            for comment_obj in section_comments:
                if comment_obj.element_path not in comments_by_path:
                    comments_by_path[comment_obj.element_path] = []
                comments_by_path[comment_obj.element_path].append(comment_obj)
        else: # Geen relevante paden gevonden in de sectie
            print(f"INFO: Geen relevante AQL paden gevonden in sectie {section_index} voor commentaren.")
    else: # current_section_data is None (kan gebeuren als total_sections = 0)
        print("INFO: Geen huidige sectie data om commentaren voor op te halen.")


    return render_template('index.html', 
                           title=f"Review: {questionnaire.get('name',{}).get('value','Vragenlijst')} - Sectie {section_index + 1}", 
                           questionnaire=questionnaire,
                           comments_by_path=comments_by_path,
                           current_section=current_section_data,
                           current_section_index=section_index,
                           total_sections=total_sections
                          )

# Je zou ook een commando kunnen toevoegen om de cache te legen/herladen, bv. via Flask CLI
# @bp.cli.command('clear-questionnaire-cache')
# def clear_cache_command():
#     global _questionnaire_cache
#     _questionnaire_cache = None
#     print("Vragenlijst cache geleegd.")