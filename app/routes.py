from flask import render_template, flash, redirect, url_for, Blueprint, request
from app import db
from app.models import Comment

bp = Blueprint('main', __name__)

def get_mock_questionnaire_data():
    # Deze functie blijft hetzelfde als voorheen voor de demo
    return {
        "_type": "COMPOSITION", "name": {"value": "Klinische Evaluatie"},
        "archetype_node_id": "openEHR-EHR-COMPOSITION.encounter.v1",
        "content": [{
            "_type": "OBSERVATION", "archetype_node_id": "openEHR-EHR-OBSERVATION.blood_pressure.v2",
            "name": {"value": "Bloeddruk"}, "data": {"_type": "HISTORY", "archetype_node_id": "at0001",
            "events": [{"_type": "POINT_EVENT", "archetype_node_id": "at0002", "data": {"_type": "ITEM_TREE",
            "archetype_node_id": "at0003", "items": [{"_type": "ELEMENT", "archetype_node_id": "at0004",
            "name": {"value": "Systolische druk"}, "value": {"_type": "DV_QUANTITY", "magnitude": 125, "units": "mm[Hg]"}},
            {"_type": "ELEMENT", "archetype_node_id": "at0005", "name": {"value": "Diastolische druk"},
            "value": {"_type": "DV_QUANTITY", "magnitude": 82, "units": "mm[Hg]"}}]}}]}
        }]
    }

@bp.route('/', methods=['GET', 'POST'])
def index():
    questionnaire = get_mock_questionnaire_data()
    
    if request.method == 'POST':
        # Haal de data uit het formulier
        comment_text = request.form.get('comment_text')
        author_name = request.form.get('author_name')
        element_path = request.form.get('element_path')
        parent_id = request.form.get('parent_id')
        
        # Valideer de input
        if comment_text and author_name and element_path:
            comment = Comment(
                comment_text=comment_text,
                author_name=author_name, # Sla de ingevulde naam op
                element_path=element_path,
                parent_id=int(parent_id) if parent_id else None
            )
            db.session.add(comment)
            db.session.commit()
            flash('Uw commentaar is geplaatst!', 'success')
            return redirect(url_for('main.index'))
        else:
            flash('Naam en commentaar zijn verplichte velden.', 'danger')

    # Haal commentaren op voor weergave
    comments_by_path = {}
    top_level_comments = Comment.query.filter_by(parent_id=None).order_by(Comment.created_at.asc()).all()
    for comment in top_level_comments:
        if comment.element_path not in comments_by_path:
            comments_by_path[comment.element_path] = []
        comments_by_path[comment.element_path].append(comment)
        
    return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path=comments_by_path)