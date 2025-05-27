from flask import render_template, flash, redirect, url_for, Blueprint, request
from flask_login import login_user, logout_user, current_user, login_required
from app import db
from app.models import User, Comment
from app.forms import LoginForm, RegistrationForm
from app.email import send_credentials_email
import string
import random

bp = Blueprint('main', __name__)

def generate_strong_password(length=12):
    characters = string.ascii_letters + string.digits + string.punctuation
    password = ''.join(random.choice(characters) for i in range(length))
    return password

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
@login_required
def index():
    questionnaire = get_mock_questionnaire_data()
    
    if request.method == 'POST':
        comment_text = request.form.get('comment_text')
        element_path = request.form.get('element_path')
        parent_id = request.form.get('parent_id')
        if comment_text and element_path:
            comment = Comment(
                comment_text=comment_text,
                element_path=element_path,
                author=current_user,
                parent_id=int(parent_id) if parent_id else None
            )
            db.session.add(comment)
            db.session.commit()
            flash('Uw commentaar is geplaatst!', 'success')
            return redirect(url_for('main.index'))

    # Haal alleen top-level commentaren op. Replies worden via het model geladen.
    comments_by_path = {}
    top_level_comments = Comment.query.filter_by(parent_id=None).order_by(Comment.created_at.asc()).all()
    for comment in top_level_comments:
        if comment.element_path not in comments_by_path:
            comments_by_path[comment.element_path] = []
        comments_by_path[comment.element_path].append(comment)
        
    return render_template('index.html', title='Review', questionnaire=questionnaire, comments_by_path=comments_by_path)

@bp.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = RegistrationForm()
    if form.validate_on_submit():
        email = form.email.data
        username = email.split('@')[0]
        # Check if username already exists, if so, append a number
        original_username = username
        counter = 1
        while User.query.filter_by(username=username).first():
            username = f"{original_username}{counter}"
            counter += 1
        
        password = generate_strong_password()
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        
        # Stuur de 'e-mail' (wordt geprint in de console)
        send_credentials_email(user, password)
        
        flash('Accountaanvraag succesvol! Controleer uw (console voor de) e-mail voor inloggegevens.', 'info')
        return redirect(url_for('main.login'))
    return render_template('register.html', title='Account Aanvragen', form=form)

@bp.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('main.index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(username=form.username.data).first()
        if user is None or not user.check_password(form.password.data):
            flash('Ongeldige gebruikersnaam of wachtwoord', 'danger')
            return redirect(url_for('main.login'))
        login_user(user)
        return redirect(url_for('main.index'))
    return render_template('login.html', title='Inloggen', form=form)

@bp.route('/logout')
def logout():
    logout_user()
    return redirect(url_for('main.index'))