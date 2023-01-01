import os
import datetime
import re
import json
import string
import shutil
import tarfile
import nbformat
import threading
from pathlib import Path
from traitlets.config import Config
from nbconvert.writers import FilesWriter
from nbconvert import SlidesExporter, MarkdownExporter
from nbconvert.preprocessors import ExecutePreprocessor

from azure.cosmos import exceptions, CosmosClient, PartitionKey
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify
from flask import Flask

UPLOAD_FOLDER = '/home/azureuser/api/raw/uploads'

endpoint = os.environ["COSMOS_ENDPOINT"]
key = os.environ["COSMOS_KEY"]
client = CosmosClient(url=endpoint, credential=key)
database = client.create_database_if_not_exists(id="manimbooks")
container_name = "books"
try:
    container = database.create_container(
        id=container_name, partition_key=PartitionKey(path="/bookName")
    )
except exceptions.CosmosResourceExistsError:
    container = database.get_container_client(container_name)


app = Flask(__name__, static_url_path='/raw',
            static_folder='/home/azureuser/api/raw')
app.secret_key = "secret key"
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024


COVER_ALLOWED_EXTENSIONS = set(['png', 'jpg', 'jpeg', 'webp'])
CHAPTER_ALLOWED_EXTENSIONS = set(['ipynb'])


def allowed_cover_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in COVER_ALLOWED_EXTENSIONS


def allowed_chapter_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in CHAPTER_ALLOWED_EXTENSIONS


def convert(book, author):

    folder = "/home/azureuser/api/raw/uploads/" + author + '/' + book
    script_dir = "/home/azureuser/api/convert"

    def get_path(s):
        return str(Path(s).expanduser().absolute().resolve())

    def dir_to_list(dirname):
        data = []
        for name in sorted(os.listdir(dirname)):
            dct = {}
            dct['name'] = name
            dct['path'] = get_path(os.path.join(dirname, name))
            full_path = os.path.join(dirname, name)
            if os.path.isfile(full_path):
                data.append(dct)
        return data

    def format_name(s, i):
        a = re.sub("ch\d", f"{i}.", s)
        b = string.capwords(a.replace("_", " "))
        return b

    # custom configuration for nbconvert
    c = Config()
    c.TemplateExporter.extra_template_basedirs
    my_templates = script_dir + '/templates'
    c.TemplateExporter.extra_template_basedirs = [my_templates]
    c.TemplateExporter.exclude_input = True
    c.SlidesExporter.theme = 'dark'
    c.SlidesExporter.reveal_theme = 'night'
    c.SlidesExporter.reveal_scroll = True
    c.FilesWriter.build_directory = f"{script_dir}/.cache/{book}"

    # initialize cache output folder
    if not os.path.exists(f"{script_dir}/.cache/"):
        os.mkdir(f"{script_dir}/.cache/")
    if not os.path.exists(c.FilesWriter.build_directory):
        os.mkdir(c.FilesWriter.build_directory)

    chapters = []

    i = 1
    for notebook in dir_to_list(get_path(folder)):
        if notebook['name'].rsplit('.', 1)[1].lower() != 'ipynb':
            continue
        dct = {}
        os.chdir(c.FilesWriter.build_directory)
        print("Converting ", notebook['name'])
        shutil.copy2(notebook['path'], c.FilesWriter.build_directory)
        filename = format_name(str(notebook['name']).replace(".ipynb", ""), i)
        dct['name'] = filename
        i += 1

        # execute (render) the contents of the notebook
        print("Executing contents of notebook...", end="    ")
        ep = ExecutePreprocessor(timeout=600)
        nb = nbformat.read(notebook['path'], nbformat.NO_CONVERT)
        ep.preprocess(nb)
        print("Done")

        # convert the notebook to slides
        print("Converting to slides...", end="    ")
        slides = SlidesExporter(config=c, template_name="reveal.js")
        (output, resources) = slides.from_notebook_node(nb)
        fw = FilesWriter(config=c)
        fw.write(output, resources, notebook_name=filename)
        dct['slides'] = filename + ".slides.html"
        print("Done")

        # convert the notebook to markdown, copy it to texme html template
        print("Converting to markdown...", end="    ")
        shutil.copy2(f"{script_dir}/templates/scroll.html",
                     f"{filename}.html")
        scroll = MarkdownExporter(config=c)
        (output, resources) = scroll.from_notebook_node(nb)
        fw = FilesWriter(config=c)
        fw.write(output, resources, notebook_name=filename)
        with open(f"{filename}.md", "r") as f, open(f"{filename}.html", "a+") as g:
            g.write(f.read())
            os.remove(f"{filename}.md")
        dct['md'] = filename + ".html"
        print("Done")
        chapters.append(dct)

    # create index.json file
    index = {
        "author": author,
        "title": book,
        "chapters": chapters
    }
    open("index.json", "w").write(json.dumps(index, indent=4))

    # create tarball
    os.chdir(get_path(f"{c.FilesWriter.build_directory}") + "/../..")

    def make_tarfile(output_filename, source_dir):
        with tarfile.open(output_filename, "w:gz") as tar:
            tar.add(source_dir, arcname=os.path.basename(source_dir))
        tar.close()

    print("Creating tarball...", end="    ")
    make_tarfile(f"{book}.mbook", f"./.cache/{book}")
    shutil.move(f"{book}.mbook", folder)
    file = tarfile.open(f"{folder}/{book}.mbook")
    file.extractall(folder)
    print("Done")

    print("Cleaning up...", end="    ")
    shutil.rmtree(f"./.cache/{book}")
    print("Done")


@app.route('/new_book', methods=['POST'])
def new_book():
    book_title = request.form.get('book_title')
    author = request.form.get('author')
    if not book_title or not author:
        resp = jsonify({'message': 'Incomplete form'})
        resp.status_code = 400
        return resp
    book_dir = app.config['UPLOAD_FOLDER'] + '/' + author + '/' + book_title
    if os.path.exists(book_dir):
        resp = jsonify({'message': 'Book already exists'})
        resp.status_code = 400
        return resp
    if 'cover' not in request.files:
        request.files
        resp = jsonify({'message': 'No cover page in the request'})
        resp.status_code = 400
        return resp
    for file in request.files:
        if file == 'cover':
            cover = request.files[file]
            if cover.filename == '':
                resp = jsonify(
                    {'message': 'No cover page selected for uploading'})
                resp.status_code = 400
                return resp
            if cover and allowed_cover_file(cover.filename):
                filename = secure_filename(cover.filename)
                if not os.path.exists(book_dir):
                    os.makedirs(book_dir)
                cover.save(os.path.join(book_dir, "cover." +
                           filename.rsplit('.', 1)[1].lower()))
            else:
                resp = jsonify(
                    {'message': 'Allowed file types are txt, png, jpg, jpeg, webp'})
                resp.status_code = 400
                return resp
        else:
            chapter = request.files[file]
            if chapter.filename == '':
                resp = jsonify(
                    {'message': 'No chapter selected for uploading'})
                resp.status_code = 400
                return resp
            if chapter and allowed_chapter_file(chapter.filename):
                filename = secure_filename(chapter.filename)
                if not os.path.exists(book_dir):
                    os.makedirs(book_dir)
                chapter.save(os.path.join(book_dir, filename))
            else:
                resp = jsonify(
                    {'message': 'Only ipynb files are allowed'})
                resp.status_code = 400
                return resp

    book = {
        'id': str(hash(author + book_title)),
        'bookName': book_title,
        'author': author,
        'timestamp': datetime.datetime.utcnow().isoformat(),
        'cover': 'cover.' + filename.rsplit('.', 1)[1].lower(),
        'status': 'converting'
    }
    container.upsert_item(book)
    convert_thread = threading.Thread(
        target=convert, args=(book_title, author))
    convert_thread.start()
    resp = jsonify({'message': 'Book successfully uploaded'})
    resp.status_code = 201
    return resp


@app.route('/get_books', methods=['GET'])
def get_books():
    query = "SELECT * FROM books ORDER BY books.timestamp DESC OFFSET 0 LIMIT 100"
    items = list(container.query_items(
        query=query,
        enable_cross_partition_query=True
    ))
    resp = jsonify(items)
    resp.status_code = 200
    return resp


if __name__ == "__main__":
    app.run()