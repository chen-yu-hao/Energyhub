"""HTTP endpoints for frontends; calculations use an external PySCF worker."""
from __future__ import annotations
import io
import json
import os
from pathlib import Path
from flask import Blueprint, Flask, current_app, jsonify, request, send_file, send_from_directory
from .job_manager import EnergyJobManager


def create_blueprint(manager=None):
    bp = Blueprint('energyhub', __name__, url_prefix='/api/energyhub')
    def service():
        return manager or current_app.extensions['energyhub_manager']

    @bp.get('/methods')
    def methods():
        try:
            return jsonify(service().capabilities(refresh=request.args.get('refresh', '').lower() in ('1', 'true')))
        except (RuntimeError, OSError) as error:
            return jsonify(error=str(error)), 503

    @bp.get('/config')
    def get_config():
        return jsonify({**service().config(refresh=request.args.get('refresh', '').lower() in ('1', 'true')), 'max_upload_bytes': current_app.config['MAX_CONTENT_LENGTH']})

    @bp.put('/config')
    def set_config():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='JSON object required'), 400
        try:
            return jsonify(service().configure(pool_size=body.get('pool_size'),
                memory_pool_mb=body.get('memory_pool_mb', body.get('memory_pool_size')),
                thread_pool_size=body.get('thread_pool_size'), resource_mode=body.get('resource_mode')))
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
        except (TypeError, ValueError) as error:
            return jsonify(error=str(error)), 400
        except OSError as error:
            return jsonify(error=f'Cannot save resource configuration: {error}'), 503

    @bp.get('/jobs')
    def list_jobs():
        try:
            return jsonify(service().list_jobs(limit=request.args.get('limit', '50'),
                offset=request.args.get('offset', '0'), query=request.args.get('q', ''), state=request.args.get('state')))
        except (TypeError, ValueError) as error:
            return jsonify(error=str(error)), 400

    @bp.post('/jobs')
    def submit_job():
        archive = request.files.get('tgz') or request.files.get('archive')
        reference = request.files.get('ref') or request.files.get('reference')
        if archive is None or reference is None:
            return jsonify(error="Multipart fields 'tgz' and 'ref' are required"), 400
        if not (archive.filename or '').lower().endswith(('.tgz', '.tar.gz')):
            return jsonify(error='Geometry filename must end with .tgz or .tar.gz'), 400
        try:
            job = service().submit(archive, reference, request.form.to_dict())
            return jsonify(job.as_dict()), 202
        except (TypeError, ValueError) as error:
            return jsonify(error=str(error)), 400
        except (RuntimeError, OSError) as error:
            return jsonify(error=str(error)), 503

    @bp.get('/jobs/<task_id>')
    def get_job(task_id):
        job = service().get(task_id)
        return (jsonify(job.as_dict()), 200) if job else (jsonify(error='Task not found'), 404)

    @bp.post('/jobs/<task_id>/cancel')
    @bp.post('/jobs/<task_id>/terminate')
    @bp.delete('/jobs/<task_id>')
    def cancel_job(task_id):
        job = service().cancel(task_id)
        return (jsonify(job.as_dict()), 200) if job else (jsonify(error='Task not found'), 404)

    @bp.get('/jobs/<task_id>/result')
    @bp.get('/jobs/<task_id>/download')
    def download(task_id):
        job = service().get(task_id)
        if job is None:
            return jsonify(error='Task not found'), 404
        if job.state != 'completed':
            return jsonify(error='Task has no complete result', state=job.state), 409
        if not job.output_path.is_file():
            return jsonify(error='Result file unavailable'), 404
        return send_file(job.output_path, as_attachment=True, download_name=job.result_filename, mimetype='text/plain')

    @bp.get('/jobs/<task_id>/report')
    def report(task_id):
        try:
            payload = service().report(task_id)
            if payload is None:
                return jsonify(error='Task not found'), 404
            if request.args.get('download', '').lower() in ('1', 'true'):
                job = service().get(task_id)
                body = json.dumps(payload, ensure_ascii=False, indent=2).encode('utf-8')
                return send_file(io.BytesIO(body), as_attachment=True,
                                 download_name=job.result_filename[:-4] + '.json', mimetype='application/json')
            return jsonify(payload)
        except RuntimeError as error:
            return jsonify(error=str(error)), 409
        except OSError as error:
            return jsonify(error=f'Report file unavailable: {error}'), 404

    @bp.get('/jobs/<task_id>/log')
    def log(task_id):
        try:
            payload = service().log_tail(task_id, lines=request.args.get('lines', '200'))
            return (jsonify(payload), 200) if payload is not None else (jsonify(error='Task not found'), 404)
        except (TypeError, ValueError) as error:
            return jsonify(error=str(error)), 400
        except OSError as error:
            return jsonify(error=f'Log file unavailable: {error}'), 503

    return bp


def register_energyhub(app, manager=None):
    manager = manager or EnergyJobManager(pool_size=os.environ.get('ENERGYHUB_POOL_SIZE'),
                                        memory_pool_mb=os.environ.get('ENERGYHUB_MEMORY_MB'),
                                        thread_pool_size=os.environ.get('ENERGYHUB_THREAD_POOL_SIZE'), resource_mode=os.environ.get('ENERGYHUB_RESOURCE_MODE'))
    app.extensions['energyhub_manager'] = manager
    app.register_blueprint(create_blueprint(manager))
    return manager


def create_app(manager=None, *, data_dir=None, pool_size=None, memory_pool_mb=None, memory_pool_size=None,
               thread_pool_size=None, resource_mode=None):
    if memory_pool_mb is None:
        memory_pool_mb = memory_pool_size
    manager = manager or EnergyJobManager(data_dir,
        os.environ.get('ENERGYHUB_POOL_SIZE') if pool_size is None else pool_size,
        os.environ.get('ENERGYHUB_MEMORY_MB') if memory_pool_mb is None else memory_pool_mb,
        thread_pool_size=os.environ.get('ENERGYHUB_THREAD_POOL_SIZE') if thread_pool_size is None else thread_pool_size,
        resource_mode=os.environ.get('ENERGYHUB_RESOURCE_MODE') if resource_mode is None else resource_mode)
    static_dir = Path(__file__).resolve().parent / 'static'
    app = Flask(__name__, static_folder=str(static_dir), static_url_path='/static')
    app.config['MAX_CONTENT_LENGTH'] = int(os.environ.get('ENERGYHUB_MAX_UPLOAD_BYTES', 64 * 1024**2))
    register_energyhub(app, manager)
    @app.get('/')
    def index():
        return send_from_directory(static_dir, 'index.html')

    @app.after_request
    def prevent_stale_api(response):
        if request.path.startswith('/api/') or request.path == '/':
            response.headers['Cache-Control'] = 'no-store'
        if request.path.startswith('/static/') and request.path.lower().endswith(('.tgz', '.tar.gz')):
            # These are archive bytes, not HTTP gzip transport encoding.
            # Otherwise browsers transparently decompress the sample before upload.
            response.headers.pop('Content-Encoding', None)
            response.headers['Content-Type'] = 'application/gzip'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response
    @app.errorhandler(413)
    def upload_too_large(error):
        return jsonify(error='Upload exceeds the configured size limit'), 413
    return app
