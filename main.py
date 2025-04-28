
import os
from pathlib import Path
from datetime import datetime, timedelta

from flask import (
    Flask, render_template, request, jsonify, url_for, redirect, flash
)
from dotenv import load_dotenv
load_dotenv()
from sqlalchemy import text, func
import openai
import traceback

from extensions import db
from models import Contact, Message, Property
from webhook_route import webhook_bp

app = Flask(__name__)

BASEDIR = Path(__file__).resolve().parent
INSTANCE = BASEDIR / "instance"
INSTANCE.mkdir(exist_ok=True)
DB_FILE = INSTANCE / "messages.db"

app.config["SQLALCHEMY_DATABASE_URI"] = os.getenv("DATABASE_URL", f"sqlite:///{DB_FILE}")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 300}
app.secret_key = os.getenv("FLASK_SECRET", "default_secret")
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True

db.init_app(app)
app.register_blueprint(webhook_bp)
openai.api_key = os.getenv("OPENAI_API_KEY")

with app.app_context():
    db.create_all()

@app.route('/delete-media', methods=['POST'])
def delete_media():
    file_path = request.form.get('file_path')
    message_id = request.form.get('message_id')

    if not file_path:
        flash("No file specified.", "warning")
        return redirect(request.referrer or url_for('gallery_view'))

    full_path = os.path.join(app.static_folder, file_path)
    if os.path.exists(full_path):
        try:
            os.remove(full_path)
            flash('File deleted successfully.', 'success')
        except Exception as e:
            flash(f'Error deleting file: {e}', 'danger')
            return redirect(request.referrer or url_for('gallery_view'))
    else:
        flash('File does not exist.', 'warning')

    if message_id:
        message = Message.query.get(message_id)
        if message and message.local_media_paths:
            paths = message.local_media_paths.split(',')
            if file_path in paths:
                paths.remove(file_path)
                message.local_media_paths = ','.join(paths) if paths else None
                db.session.commit()

    return redirect(request.referrer or url_for('gallery_view'))

@app.route("/")
def index():
    current_year = datetime.utcnow().year
    return render_template("index.html", current_year=current_year)

@app.route("/gallery")
def gallery_view():
    upload_folder = os.path.join(app.static_folder, "uploads")
    image_files = [f"uploads/{fn}" for fn in os.listdir(upload_folder)
                   if os.path.splitext(fn)[1].lower() in {".jpg", ".jpeg", ".png", ".gif", ".webp"}]
    return render_template("gallery.html", image_files=image_files, property=None, current_year=datetime.utcnow().year)

# URL Map Debug
with app.app_context():
    print("\n--- URL MAP ---")
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.endpoint):
        methods = ",".join(sorted(rule.methods - {"HEAD", "OPTIONS"}))
        print(f"{rule.endpoint:25} {methods:15} {rule.rule}")
    print("--- END URL MAP ---\n")

if __name__ == "__main__":
    app.run(debug=True)
