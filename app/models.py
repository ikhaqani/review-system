from app import db

class Comment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    element_path = db.Column(db.String(500), nullable=False)
    comment_text = db.Column(db.Text, nullable=False)
    author_name = db.Column(db.String(120), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    parent_id = db.Column(db.Integer, db.ForeignKey('comment.id'), nullable=True)
    
    replies = db.relationship('Comment', backref=db.backref('parent', remote_side=[id]), lazy='dynamic')