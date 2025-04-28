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
        return redirect(request.referrer or url_for('gallery_view'))

    # Update message record if message_id is provided
    if message_id:
        message = Message.query.get(message_id)
        if message and message.local_media_paths:
            paths = message.local_media_paths.split(',')
            if file_path in paths:
                paths.remove(file_path)
                message.local_media_paths = ','.join(paths) if paths else None
                db.session.commit()

    return redirect(request.referrer or url_for('gallery_view'))
