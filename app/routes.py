from flask import render_template, flash, redirect, url_for, Blueprint, request
from app import db
from app.models import Comment
import json
import os

bp = Blueprint('main', __name__)

# Pad naar uw Web Template JSON-bestand
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
TEMPLATE_FILEPATH = os.path.join(BASE_DIR, 'templates_openehr', 'ACP-DUTCH.json')

def load_web_template_json(filepath):
    """Laadt en parset het Web Template JSON-bestand."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            template_data = json.load(f)
        return template_data
    except FileNotFoundError:
        print(f"FOUT: Bestand niet gevonden: {filepath}")
        flash(f"Template bestand niet gevonden: {filepath}. Zorg dat het in de map app/templates_openehr/ staat.", "danger")
        return None
    except json.JSONDecodeError as e:
        print(f"FOUT: Fout bij het parsen van JSON in {filepath}: {e}")
        flash(f"Fout in template JSON-bestand: {e}. Controleer de JSON-syntax.", "danger")
        return None

def _create_value_structure(node_json):
    """
    Hulpfunctie om dummy/placeholder value-structuren te maken
    gebaseerd op het input type uit de Web Template.
    """
    # Default value
    value_structure = {"_type": "DV_TEXT", "value": "[Nog niet ingevuld]"}
    
    inputs = node_json.get('inputs', [])
    if inputs and isinstance(inputs, list) and len(inputs) > 0:
        input_def = inputs[0] # Neem de eerste input definitie
        input_type = input_def.get('type', '').upper()
        
        if input_type == 'TEXT' or input_type == 'STRING':
            value_structure = {"_type": "DV_TEXT", "value": ""}
        elif input_type == 'CODED_TEXT':
            options = []
            for option_item in input_def.get('list', []):
                options.append({
                    "label": option_item.get('localizedLabels', {}).get('en', option_item.get('label', 'Optie')), # Voorkeur voor gelokaliseerd label
                    "value": option_item.get('value')
                })
            value_structure = {
                "_type": "DV_CODED_TEXT", 
                "value": "", # Leeg voor selectie
                "defining_code": {"code_string": "", "terminology_id": {"value": input_def.get('terminology', '')}},
                "options": options
            }
        elif input_type == 'BOOLEAN':
            value_structure = {"_type": "DV_BOOLEAN", "value": None} # True, False, of None voor niet gekozen
        elif input_type == 'INTEGER' or input_type == 'COUNT':
            value_structure = {"_type": "DV_COUNT", "magnitude": None}
        elif input_type == 'DECIMAL' or input_type == 'REAL' or input_type == 'DOUBLE': # Vaak binnen DV_QUANTITY
             # Dit is een vereenvoudiging; DV_QUANTITY heeft magnitude EN units
             # Als 'inputs' direct een DV_QUANTITY definieert, moet dit anders.
             # Aanname: als type DECIMAL is, is het een magnitude voor een quantity.
            value_structure = {"_type": "DV_QUANTITY", 
                               "magnitude": None, 
                               "units": input_def.get('units', '')} # Units kunnen in constraints staan
        elif input_type == 'DATETIME':
            value_structure = {"_type": "DV_DATE_TIME", "value": None} # YYYY-MM-DDTHH:MM:SS
        elif input_type == 'DATE':
            value_structure = {"_type": "DV_DATE", "value": None} # YYYY-MM-DD
        elif input_type == 'TIME':
            value_structure = {"_type": "DV_TIME", "value": None} # HH:MM:SS
        elif input_type == 'DV_QUANTITY': # Als het type zelf DV_QUANTITY is
            value_structure = {"_type": "DV_QUANTITY", 
                               "magnitude": None, 
                               "units": input_def.get('constraints', {}).get('units', [])[0] if input_def.get('constraints', {}).get('units') else ''}


    # Specifiek voor uw data, als rmType DV_CODED_TEXT is
    # maar het 'type' in 'inputs' niet CODED_TEXT is (wat vreemd is, maar kan)
    if node_json.get('rmType') == 'DV_CODED_TEXT' and value_structure['_type'] != 'DV_CODED_TEXT':
        options = []
        # Als de opties direct onder de node staan (niet in 'inputs')
        for option_item in node_json.get('list', []): # Sommige web templates hebben 'list' direct
             options.append({
                "label": option_item.get('localizedLabels', {}).get('en', option_item.get('label', 'Optie')),
                "value": option_item.get('value')
            })
        value_structure = {
            "_type": "DV_CODED_TEXT", 
            "value": "", 
            "defining_code": {"code_string": "", "terminology_id": {"value": node_json.get('terminology', '')}},
            "options": options
        }
        
    return value_structure

def _extract_elements_recursive(current_node_json, lang='en'):
    """
    Recursief door de Web Template node en zijn kinderen,
    en extraheert alleen RM_TYPE 'ELEMENT' nodes in een platte lijst,
    geformatteerd voor de Flask template.
    Negeert 'inContext' true nodes die vaak metadata zijn.
    """
    elements_for_flask = []
    
    # Sla metadata-nodes over die vaak 'inContext: true' hebben in sommige templates
    if current_node_json.get('inContext') is True:
        return elements_for_flask

    node_rm_type = current_node_json.get('rmType', '').upper()

    if node_rm_type == 'ELEMENT' or \
       (node_rm_type.startswith('DV_') and node_rm_type not in ['DV_INTERVAL']): # Ook DV_TEXT, DV_QUANTITY etc. direct behandelen als ELEMENT
        
        name_obj = current_node_json.get('localizedNames', {})
        # Probeer eerst gelokaliseerde naam, dan 'localizedName', dan 'name'
        element_name = name_obj.get(lang, current_node_json.get('localizedName', current_node_json.get('name', "Naamloos Element")))
        
        # Voor archetype_node_id: gebruik 'id' (vaak atCode of uniek element ID)
        # of 'nodeId' als 'id' niet beschrijvend is.
        # Dit is een heuristiek; inspecteer uw JSON voor de beste source.
        display_node_id = current_node_json.get('id', current_node_json.get('nodeId', 'onbekend_id'))

        # AQL path is cruciaal. Web templates geven vaak het pad naar de waarde.
        # Dit is de sleutel voor het koppelen van commentaren.
        aql_path_for_comments = current_node_json.get('aqlPath', '')
        # Als aqlPath leeg is, probeer een pad op te bouwen (zeer basaal)
        # Dit is een zwak punt en moet wellicht worden verfijnd op basis van uw JSON.
        if not aql_path_for_comments:
            aql_path_for_comments = f"placeholder/path/to/{display_node_id}" # Noodoplossing

        value_structure = _create_value_structure(current_node_json)

        elements_for_flask.append({
            "_type": "ELEMENT",
            "archetype_node_id": display_node_id,
            "name": {"value": element_name},
            "value": value_structure,
            "element_path_for_comments": aql_path_for_comments
        })

    # Als de huidige node kinderen heeft, ga dan recursief verder
    if 'children' in current_node_json and isinstance(current_node_json.get('children'), list):
        for child_node in current_node_json['children']:
            elements_for_flask.extend(_extract_elements_recursive(child_node, lang))
            
    return elements_for_flask

def transform_web_template_to_questionnaire(web_template_data):
    if not web_template_data or 'tree' not in web_template_data:
        return {"_type": "COMPOSITION", "name": {"value": "Fout: Ongeldige template data"}, "content": []}

    lang = web_template_data.get('defaultLanguage', 'en')
    root_tree = web_template_data['tree']

    name_obj = root_tree.get('localizedNames', {})
    composition_name = name_obj.get(lang, root_tree.get('localizedName', root_tree.get('name', "Onbekende Vragenlijst")))
    composition_node_id = root_tree.get('nodeId', root_tree.get('archetypeId', 'unknown.v1'))

    content_list_for_flask = []

    # Verwerk de directe kinderen van de COMPOSITION tree.
    # Dit zijn meestal SECTION, OBSERVATION, EVALUATION, INSTRUCTION, ACTION, ADMIN_ENTRY, of EVENT_CONTEXT.
    for top_level_node_json in root_tree.get('children', []):
        top_level_rm_type = top_level_node_json.get('rmType', '').upper()
        
        # Haal de naam van de top-level node
        name_obj = top_level_node_json.get('localizedNames', {})
        section_name = name_obj.get(lang, top_level_node_json.get('localizedName', top_level_node_json.get('name', "Naamloze Sectie")))
        
        # Gebruik 'nodeId' als het bestaat en relevant is, anders 'id'.
        # 'archetypeId' kan ook een veld zijn in uw JSON.
        section_archetype_id = top_level_node_json.get('nodeId', top_level_node_json.get('id', f"section_{len(content_list_for_flask)}"))
        if not section_archetype_id: # Fallback als beide leeg zijn
            section_archetype_id = top_level_node_json.get('archetypeId', f"section_{len(content_list_for_flask)}")


        # De EVENT_CONTEXT bevat vaak metadata, maar kan ook displayable items bevatten via CLUSTERs.
        # Uw ACP-DUTCH.json structuur voor context: tree -> children (EVENT_CONTEXT) -> children (CLUSTERs) -> children (ELEMENTs)
        if top_level_rm_type == 'EVENT_CONTEXT':
            for context_child_node in top_level_node_json.get('children', []): # Dit zijn CLUSTERs
                if context_child_node.get('rmType','').upper() not in ['CLUSTER', 'ELEMENT'] : # Alleen CLUSTERs of ELEMENTs hier verwerken als secties
                    continue

                cluster_name_obj = context_child_node.get('localizedNames', {})
                cluster_name = cluster_name_obj.get(lang, context_child_node.get('localizedName', context_child_node.get('name', "Context Item")))
                cluster_archetype_id = context_child_node.get('nodeId', context_child_node.get('id', 'unknown_cluster'))
                
                elements = _extract_elements_recursive(context_child_node, lang)
                if elements:
                    content_list_for_flask.append({
                        "_type": context_child_node.get('rmType', 'SECTION'), # Gebruik CLUSTER rmType, of SECTION
                        "archetype_node_id": cluster_archetype_id,
                        "name": {"value": cluster_name},
                        "data": {"_type": "ITEM_TREE", "items": elements} # Vlakkere structuur
                    })
            continue # Verwerkt, ga naar volgende top-level node

        # Voor andere top-level nodes (SECTION, OBSERVATION, EVALUATION, etc.)
        elements = _extract_elements_recursive(top_level_node_json, lang)

        if not elements: # Sla secties over die geen displayable elementen opleveren
            print(f"INFO: Geen elementen geëxtraheerd voor sectie: {section_name} (Type: {top_level_rm_type})")
            continue

        content_item = {}
        # De index.html template verwacht voor OBSERVATION een diepere 'events' structuur.
        if top_level_rm_type == "OBSERVATION":
            content_item = {
                "_type": "OBSERVATION",
                "archetype_node_id": section_archetype_id,
                "name": {"value": section_name},
                "data": {"_type": "HISTORY", "events": [{"_type": "POINT_EVENT", "data": {
                            "_type": "ITEM_TREE", "items": elements
                        }}]}
            }
        else: # Voor EVALUATION, SECTION, INSTRUCTION, ACTION, ADMIN_ENTRY, etc.
              # gebruiken we de vlakkere structuur die index.html ook aankan.
            content_item = {
                "_type": top_level_rm_type if top_level_rm_type else "SECTION", # Fallback naar SECTION
                "archetype_node_id": section_archetype_id,
                "name": {"value": section_name},
                "data": {"_type": "ITEM_TREE", "items": elements}
            }
        
        content_list_for_flask.append(content_item)

    return {
        "_type": "COMPOSITION",
        "name": {"value": composition_name},
        "archetype_node_id": composition_node_id,
        "content": content_list_for_flask
    }

def get_questionnaire_structure_from_template():
    """Hoofdfunctie om de questionnaire structuur te krijgen."""
    web_template_data = load_web_template_json(TEMPLATE_FILEPATH)
    if web_template_data is None:
        return {
            "_type": "COMPOSITION",
            "name": {"value": "FOUT: Template bestand niet geladen of corrupt"},
            "archetype_node_id": "error.v1",
            "content": []
        }
    
    questionnaire_structure = transform_web_template_to_questionnaire(web_template_data)
    return questionnaire_structure

@bp.route('/', methods=['GET', 'POST'])
def index():
    questionnaire = get_questionnaire_structure_from_template() # GEWIJZIGD
    
    if request.method == 'POST':
        comment_text = request.form.get('comment_text')
        author_name = request.form.get('author_name')
        element_path = request.form.get('element_path')
        parent_id = request.form.get('parent_id')
        
        if comment_text and author_name and element_path:
            comment = Comment(
                comment_text=comment_text,
                author_name=author_name,
                element_path=element_path,
                parent_id=int(parent_id) if parent_id else None
            )
            db.session.add(comment)
            db.session.commit()
            flash('Uw commentaar is geplaatst!', 'success')
            return redirect(url_for('main.index'))
        else:
            flash('Naam en commentaar zijn verplichte velden.', 'danger')

    # Haal commentaren op
    # We halen alle comments op en de template zal ze nesten op basis van parent_id
    comments_by_path = {}
    all_db_comments = Comment.query.order_by(Comment.created_at.asc()).all()
    
    # Filter eerst top-level comments per pad
    for comment in all_db_comments:
        if comment.parent_id is None: # Alleen top-level comments toevoegen aan de dictionary
            if comment.element_path not in comments_by_path:
                comments_by_path[comment.element_path] = []
            comments_by_path[comment.element_path].append(comment)
            
    # De replies worden via de 'comment.replies' relatie in de template afgehandeld (render_comment macro)
        
    return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path=comments_by_path)