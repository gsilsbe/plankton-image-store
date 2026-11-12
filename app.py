#!/usr/bin/env python3
"""
Image API backed by MongoDB GridFS, with user logins.

Every endpoint except the login page requires a logged-in user.
Uploads require an instrument type (IFCB, Planktoscope or Flowcam). The gallery has
three dropdowns: Instrument type, Classes (species, from `object_annotation_category`)
and Annotation status (`object_annotation_status`). Each image's `contributor` is
taken from `object_annotation_person_name` (or the uploader's username if blank).

Run:
    uvicorn app:app --host 0.0.0.0 --port 8000

First admin account: set ADMIN_USERNAME and ADMIN_PASSWORD (see docker-compose.yml).
It is created on startup only if no users exist yet. More users: the admin can add
them in the web page, via POST /users, or with `python manage_users.py add <name>`.

Zip layout it understands (a single wrapper folder at the top is fine):

    dataset.zip
    ├── cats/
    │   ├── metadata.tsv        <- any .tsv inside the class folder, or...
    │   ├── img001.jpg
    │   └── img002.png
    ├── dogs.tsv                <- ...a .tsv next to the folder, named after it
    └── dogs/
        └── ...
"""
import csv
import io
import json
import mimetypes
import os
import posixpath
import secrets
import tempfile
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import gridfs
from bson import ObjectId
from bson.errors import InvalidId
from fastapi import (BackgroundTasks, Depends, FastAPI, File, Form, HTTPException,
                     Request, Response, UploadFile)
from fastapi.responses import HTMLResponse, StreamingResponse
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

MONGO_URI = os.environ.get("MONGO_URI", "mongodb://localhost:27017")
DB_NAME = os.environ.get("IMAGE_DB", "image_db")
UPLOAD_TMP = os.environ.get("UPLOAD_TMP", tempfile.gettempdir())
SESSION_DAYS = int(os.environ.get("SESSION_DAYS", "7"))
COOKIE_SECURE = os.environ.get("COOKIE_SECURE", "false").lower() == "true"  # set true behind HTTPS
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
KEY_COLUMN_HINTS = ("filename", "file_name", "file", "image", "image_name", "img", "name", "path", "id")
CONTRIBUTOR_FIELD = "contributor"
# Instrument type chosen by the user at upload time
INSTRUMENTS = [i.strip() for i in os.environ.get("INSTRUMENTS", "IFCB,Planktoscope,Flowcam").split(",") if i.strip()]
# Images stored before instrument type existed are tagged with this one on startup
LEGACY_INSTRUMENT = os.environ.get("LEGACY_INSTRUMENT", "Flowcam")
# Metadata columns behind the "Classes" (species) and "Annotation status" dropdowns
CLASS_FIELD = os.environ.get("CLASS_FIELD", "object_annotation_category")
STATUS_FIELD = os.environ.get("STATUS_FIELD", "object_annotation_status")
# Statuses always offered in the "change status" list (plus any others found in the data)
STATUS_CHOICES = [x.strip() for x in os.environ.get("STATUS_CHOICES", "validated,predicted,dubious").split(",") if x.strip()]
MAX_COMMENT = 2000
# The contributor is taken from this metadata field (falls back to the uploader's username if blank)
PERSON_FIELD = os.environ.get("PERSON_FIELD", "object_annotation_person_name")

client = MongoClient(MONGO_URI)
db = client[DB_NAME]
fs = gridfs.GridFS(db, collection="images")
files_col = db["images.files"]
jobs = db["jobs"]
users = db["users"]
sessions = db["sessions"]
history = db["annotation_history"]   # one document per status change / comment

# One-time upgrades of images stored by earlier versions of this app:
#  - the zip folder name used to be saved as metadata.class; it is now metadata.folder
#  - images without an instrument type get LEGACY_INSTRUMENT
for _doc in files_col.find({"metadata.class": {"$exists": True}, "metadata.folder": {"$exists": False}},
                          {"metadata.class": 1}):
    files_col.update_one({"_id": _doc["_id"]}, {"$set": {"metadata.folder": _doc["metadata"]["class"]},
                                                "$unset": {"metadata.class": ""}})
files_col.update_many({"metadata.instrument": {"$exists": False}},
                      {"$set": {"metadata.instrument": LEGACY_INSTRUMENT}})

files_col.create_index([("metadata.dataset", ASCENDING), ("metadata.folder", ASCENDING),
                        ("metadata.uploaded_by", ASCENDING), ("filename", ASCENDING)])
files_col.create_index("metadata.instrument")
files_col.create_index("metadata.contributor")
files_col.create_index("metadata.uploaded_by")
files_col.create_index(f"metadata.attributes.{CLASS_FIELD}")
files_col.create_index(f"metadata.attributes.{STATUS_FIELD}")
users.create_index("username", unique=True)
sessions.create_index("expires_at", expireAfterSeconds=0)  # MongoDB deletes expired sessions
history.create_index([("image_id", ASCENDING), ("changed_at", DESCENDING)])
history.create_index([("changed_at", DESCENDING)])
history.create_index("user")

app = FastAPI(title="Image Store API")


# ---------------------------------------------------------------- users & auth

def now():
    return datetime.now(timezone.utc)


def hash_password(pw: str) -> bytes:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt())


def check_new_password(pw: str):
    if len(pw) < 8:
        raise HTTPException(400, "Password must be at least 8 characters")
    if len(pw.encode()) > 72:
        raise HTTPException(400, "Password must be at most 72 bytes")


# Roles, from least to most access:
#   user      - browse images, upload, read annotation history
#   annotator - also change annotation status and add comments
#   admin     - everything, including managing users and deleting anyone's images
ROLES = ["user", "annotator", "admin"]
ROLE_LABELS = {"user": "Regular user", "annotator": "Annotator", "admin": "Admin"}


def check_role(role: Optional[str]) -> str:
    role = (role or "user").strip().lower()
    if role not in ROLES:
        raise HTTPException(400, f"Role must be one of: {', '.join(ROLES)}")
    return role


def role_of(u: dict) -> str:
    return u.get("role") or ("admin" if u.get("is_admin") else "user")


def public_user(u: dict) -> dict:
    role = role_of(u)
    return {"username": u["username"], "role": role, "is_admin": role == "admin",
            "can_annotate": role in ("annotator", "admin")}


def create_user(username: str, password: str, role: str = "user"):
    username = username.strip()
    if not username or not all(c.isalnum() or c in "._-@" for c in username):
        raise HTTPException(400, "Username may contain letters, numbers and . _ - @ only")
    role = check_role(role)
    check_new_password(password)
    try:
        users.insert_one({"username": username, "password_hash": hash_password(password),
                          "role": role, "is_admin": role == "admin", "created_at": now()})
    except DuplicateKeyError:
        raise HTTPException(409, f"User '{username}' already exists")
    return public_user({"username": username, "role": role})


def set_role(username: str, role: str):
    role = check_role(role)
    u = users.find_one({"username": username})
    if not u:
        raise HTTPException(404, "User not found")
    if role_of(u) == "admin" and role != "admin" and \
            users.count_documents({"$or": [{"role": "admin"}, {"role": {"$exists": False}, "is_admin": True}]}) <= 1:
        raise HTTPException(400, "Can't remove the last admin")
    users.update_one({"_id": u["_id"]}, {"$set": {"role": role, "is_admin": role == "admin"}})
    return public_user({"username": username, "role": role})


# Accounts created before roles existed: admins stay admins, everyone else becomes a regular user
for _u in users.find({"role": {"$exists": False}}, {"is_admin": 1}):
    users.update_one({"_id": _u["_id"]}, {"$set": {"role": "admin" if _u.get("is_admin") else "user"}})


@app.on_event("startup")
def bootstrap_admin():
    name, pw = os.environ.get("ADMIN_USERNAME"), os.environ.get("ADMIN_PASSWORD")
    if name and pw and users.count_documents({}) == 0:
        create_user(name, pw, role="admin")
        print(f"Created admin user '{name}'")


def current_user(request: Request) -> dict:
    token = request.cookies.get("session")
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
    if not token:
        raise HTTPException(401, "Not logged in", headers={"WWW-Authenticate": "Bearer"})
    s = sessions.find_one({"_id": token})
    exp = s and s["expires_at"]
    if exp is not None and exp.tzinfo is None:
        exp = exp.replace(tzinfo=timezone.utc)
    if not s or exp < now():
        raise HTTPException(401, "Session expired, please log in again", headers={"WWW-Authenticate": "Bearer"})
    user = users.find_one({"username": s["username"]}, {"password_hash": 0})
    if not user:
        raise HTTPException(401, "User no longer exists")
    user["role"] = role_of(user)
    user["is_admin"] = user["role"] == "admin"
    return user


def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admins only")
    return user


def annotator_user(user: dict = Depends(current_user)) -> dict:
    if user["role"] not in ("annotator", "admin"):
        raise HTTPException(403, "Only annotators and admins can change annotations")
    return user


@app.post("/login")
def login(response: Response, username: str = Form(...), password: str = Form(...)):
    u = users.find_one({"username": username.strip()})
    if not u or not bcrypt.checkpw(password.encode()[:72], u["password_hash"]):
        raise HTTPException(401, "Wrong username or password")
    token = secrets.token_urlsafe(32)
    sessions.insert_one({"_id": token, "username": u["username"], "created_at": now(),
                         "expires_at": now() + timedelta(days=SESSION_DAYS)})
    response.set_cookie("session", token, httponly=True, samesite="lax", secure=COOKIE_SECURE,
                        max_age=SESSION_DAYS * 86400)
    return {**public_user(u), "token": token}


@app.post("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get("session")
    if token:
        sessions.delete_one({"_id": token})
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/me")
def me(user: dict = Depends(current_user)):
    return public_user(user)


@app.post("/me/password")
def change_password(current_password: str = Form(...), new_password: str = Form(...),
                    user: dict = Depends(current_user)):
    u = users.find_one({"_id": user["_id"]})
    if not bcrypt.checkpw(current_password.encode()[:72], u["password_hash"]):
        raise HTTPException(401, "Current password is wrong")
    check_new_password(new_password)
    users.update_one({"_id": u["_id"]}, {"$set": {"password_hash": hash_password(new_password)}})
    return {"ok": True}


@app.get("/roles")
def list_roles():
    return [{"role": r, "label": ROLE_LABELS[r]} for r in ROLES]


@app.get("/users")
def list_users(_: dict = Depends(admin_user)):
    return [public_user(u) for u in users.find({}, {"password_hash": 0}).sort("username", 1)]


@app.post("/users", status_code=201)
def add_user(username: str = Form(...), password: str = Form(...),
             role: Optional[str] = Form(None, description="user, annotator or admin (default user)"),
             is_admin: bool = Form(False, description="Deprecated: same as role=admin"),
             _: dict = Depends(admin_user)):
    return create_user(username, password, "admin" if is_admin and not role else (role or "user"))


@app.post("/users/{username}/role")
def change_role(username: str, role: str = Form(..., description="user, annotator or admin"),
                admin: dict = Depends(admin_user)):
    if username == admin["username"] and check_role(role) != "admin":
        raise HTTPException(400, "You can't remove your own admin role")
    return set_role(username, role)


@app.post("/users/{username}/password")
def reset_password(username: str, new_password: str = Form(...), _: dict = Depends(admin_user)):
    if not users.find_one({"username": username}, {"_id": 1}):
        raise HTTPException(404, "User not found")
    check_new_password(new_password)
    users.update_one({"username": username}, {"$set": {"password_hash": hash_password(new_password)}})
    sessions.delete_many({"username": username})
    return {"ok": True}


@app.delete("/users/{username}")
def remove_user(username: str, admin: dict = Depends(admin_user)):
    if username == admin["username"]:
        raise HTTPException(400, "You can't delete your own account")
    if not users.delete_one({"username": username}).deleted_count:
        raise HTTPException(404, "User not found")
    sessions.delete_many({"username": username})
    return {"deleted": username, "note": "Their images are kept"}


# ---------------------------------------------------------------- helpers

def oid(value: str) -> ObjectId:
    try:
        return ObjectId(value)
    except (InvalidId, TypeError):
        raise HTTPException(404, "Image not found")


def convert(value):
    """Turn TSV strings into int/float where possible; blanks become None."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    v = value.strip()
    if v == "":
        return None
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    return v


def is_junk(path: str) -> bool:
    name = posixpath.basename(path)
    return path.startswith("__MACOSX/") or "/__MACOSX/" in path or name.startswith("._") or name.startswith(".")


def norm_key(value: str) -> str:
    return posixpath.basename(str(value).strip().replace("\\", "/")).lower()


def load_tsv(zf: zipfile.ZipFile, info: zipfile.ZipInfo, image_names, key_column, warnings):
    """Read a TSV and return {normalized filename or stem: row dict}."""
    raw = zf.read(info)
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    reader = csv.DictReader(io.StringIO(text), delimiter="\t")
    rows = [r for r in reader if any((v or "").strip() for v in r.values() if isinstance(v, str))]
    # EcoTaxa-style files have a second header row of column types like [t] / [f]: skip it
    rows = [r for r in rows if not all((v or "").strip().lower() in ("[t]", "[f]", "")
                                       for v in r.values() if isinstance(v, str))]
    columns = reader.fieldnames or []
    if not rows:
        warnings.append(f"{info.filename}: no rows")
        return {}

    names = {n.lower() for n in image_names}
    stems = {posixpath.splitext(n)[0] for n in names}

    def score(col):
        return sum(1 for r in rows if (k := norm_key(r.get(col) or "")) in names or k in stems)

    if key_column:
        if key_column not in columns:
            warnings.append(f"{info.filename}: key column '{key_column}' not found (columns: {columns})")
            return {}
        key = key_column
    else:
        scored = sorted(columns, key=lambda c: (score(c), c.lower() in KEY_COLUMN_HINTS), reverse=True)
        key = scored[0] if scored and score(scored[0]) > 0 else None
        if key is None:
            warnings.append(f"{info.filename}: couldn't find a column matching image file names (columns: {columns})")
            return {}

    out = {}
    for r in rows:
        k = norm_key(r.get(key) or "")
        if not k:
            continue
        attrs = {c: convert(v) for c, v in r.items() if c is not None}
        out[k] = attrs
        out.setdefault(posixpath.splitext(k)[0], attrs)
    return out


def serialize(f: dict) -> dict:
    m = f.get("metadata") or {}
    return {
        "id": str(f["_id"]),
        "filename": f.get("filename"),
        "instrument": m.get("instrument"),
        "class": (m.get("attributes") or {}).get(CLASS_FIELD),
        "status": (m.get("attributes") or {}).get(STATUS_FIELD),
        "folder": m.get("folder"),
        "dataset": m.get("dataset"),
        "contributor": m.get("contributor"),
        "uploaded_by": m.get("uploaded_by", m.get("contributor")),
        "contentType": m.get("contentType"),
        "size": f.get("length"),
        "uploadDate": f.get("uploadDate"),
        "attributes": m.get("attributes", {}),
        "history_count": m.get("history_count", 0),
        "last_change": m.get("last_change"),
        "url": f"/images/{f['_id']}/file",
    }


def check_instrument(value: Optional[str]) -> str:
    for i in INSTRUMENTS:
        if (value or "").strip().lower() == i.lower():
            return i
    raise HTTPException(400, f"Instrument type must be one of: {', '.join(INSTRUMENTS)}")


def contributor_for(attrs, uploader: str) -> str:
    person = str((attrs or {}).get(PERSON_FIELD) or "").strip()
    return person or uploader


def store_image(src, name, dataset, instrument, folder, attrs, uploader, **extra):
    attrs = dict(attrs or {})
    contributor = contributor_for(attrs, uploader)
    attrs[CONTRIBUTOR_FIELD] = contributor
    return fs.put(
        src,
        filename=name,
        metadata={
            "dataset": dataset,
            "instrument": instrument,
            "folder": folder,                # folder inside the zip the image came from
            "contributor": contributor,      # who annotated it (from PERSON_FIELD)
            "uploaded_by": uploader,         # logged-in user who uploaded it (controls delete rights)
            "contentType": mimetypes.guess_type(name)[0] or "application/octet-stream",
            "attributes": attrs,
            "uploadedAt": now(),
            **extra,
        },
    )


def can_modify(user: dict, f: dict) -> bool:
    m = f.get("metadata") or {}
    return user.get("is_admin") or m.get("uploaded_by", m.get("contributor")) == user["username"]


# ---------------------------------------------------------------- zip ingest

def process_zip(job_id: str, path: str, dataset: str, instrument: str, key_column: Optional[str], uploader: str):
    counts = {"stored": 0, "without_metadata": 0, "skipped_duplicates": 0, "errors": 0}
    warnings = []
    jobs.update_one({"_id": job_id}, {"$set": {"status": "running", "started_at": now()}})
    try:
        with zipfile.ZipFile(path) as zf:
            images_by_dir, tsvs_by_dir = {}, {}
            for info in zf.infolist():
                if info.is_dir() or is_junk(info.filename):
                    continue
                d, name = posixpath.split(info.filename)
                ext = posixpath.splitext(name)[1].lower()
                if ext in IMAGE_EXTS:
                    images_by_dir.setdefault(d, []).append(info)
                elif ext == ".tsv":
                    tsvs_by_dir.setdefault(d, []).append(info)

            total = sum(len(v) for v in images_by_dir.values())
            jobs.update_one({"_id": job_id}, {"$set": {"total": total, "folders": len(images_by_dir)}})

            done = 0
            for d, infos in sorted(images_by_dir.items()):
                folder = posixpath.basename(d) or "(top level)"
                tsv_infos = tsvs_by_dir.get(d) or [
                    t for t in tsvs_by_dir.get(posixpath.dirname(d), [])
                    if posixpath.splitext(posixpath.basename(t.filename))[0] == folder
                ]
                if not tsv_infos:
                    warnings.append(f"{folder}: no TSV found")

                image_names = [posixpath.basename(i.filename) for i in infos]
                meta_rows = {}
                for t in tsv_infos:
                    meta_rows.update(load_tsv(zf, t, image_names, key_column, warnings))

                for info in infos:
                    name = posixpath.basename(info.filename)
                    try:
                        attrs = meta_rows.get(name.lower()) or meta_rows.get(posixpath.splitext(name)[0].lower())
                        if files_col.find_one({"metadata.dataset": dataset, "metadata.folder": folder,
                                               "metadata.uploaded_by": uploader, "filename": name}, {"_id": 1}):
                            counts["skipped_duplicates"] += 1
                        else:
                            if attrs is None:
                                counts["without_metadata"] += 1
                            with zf.open(info) as src:
                                store_image(src, name, dataset, instrument, folder, attrs, uploader,
                                            sourcePath=info.filename, job_id=job_id)
                            counts["stored"] += 1
                    except Exception as e:  # keep going; record the failure
                        counts["errors"] += 1
                        warnings.append(f"{info.filename}: {e}")
                    done += 1
                    if done % 50 == 0:
                        jobs.update_one({"_id": job_id}, {"$set": {"processed": done, "counts": counts}})

        jobs.update_one({"_id": job_id}, {"$set": {
            "status": "done", "processed": done, "counts": counts,
            "warnings": warnings[:200], "finished_at": now()}})
    except Exception as e:
        jobs.update_one({"_id": job_id}, {"$set": {
            "status": "failed", "error": str(e), "counts": counts,
            "warnings": warnings[:200], "finished_at": now()}})
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


# ---------------------------------------------------------------- image endpoints

@app.post("/datasets", status_code=202)
async def upload_dataset(
    background: BackgroundTasks,
    file: UploadFile = File(..., description="Zip of image folders + TSVs"),
    instrument: str = Form(..., description="Instrument type: IFCB, Planktoscope or Flowcam"),
    dataset: Optional[str] = Form(None, description="Dataset name (defaults to zip name)"),
    key_column: Optional[str] = Form(None, description="TSV column holding image file names"),
    user: dict = Depends(current_user),
):
    instrument = check_instrument(instrument)
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(400, "Please upload a .zip file")
    dataset = dataset or posixpath.splitext(posixpath.basename(file.filename))[0]
    job_id = uuid.uuid4().hex
    tmp_path = os.path.join(UPLOAD_TMP, f"upload_{job_id}.zip")

    # Stream to disk in 8 MB chunks so large zips never sit in memory
    with open(tmp_path, "wb") as out:
        while chunk := await file.read(8 * 1024 * 1024):
            out.write(chunk)
    if not zipfile.is_zipfile(tmp_path):
        os.remove(tmp_path)
        raise HTTPException(400, "File is not a valid zip archive")

    jobs.insert_one({"_id": job_id, "status": "queued", "dataset": dataset, "instrument": instrument,
                     "uploaded_by": user["username"],
                     "zip": file.filename, "created_at": now(), "processed": 0})
    background.add_task(process_zip, job_id, tmp_path, dataset, instrument, key_column, user["username"])
    return {"job_id": job_id, "dataset": dataset, "instrument": instrument, "uploaded_by": user["username"], "status_url": f"/jobs/{job_id}"}


@app.get("/jobs/{job_id}")
def job_status(job_id: str, user: dict = Depends(current_user)):
    job = jobs.find_one({"_id": job_id})
    if not job or not (user.get("is_admin") or job.get("uploaded_by", job.get("contributor")) == user["username"]):
        raise HTTPException(404, "Job not found")
    job["job_id"] = job.pop("_id")
    return job


@app.post("/images", status_code=201)
async def upload_image(
    file: UploadFile = File(...),
    instrument: str = Form(..., description="Instrument type: IFCB, Planktoscope or Flowcam"),
    dataset: str = Form("manual"),
    metadata: Optional[str] = Form(None, description='JSON object of metadata, e.g. {"object_annotation_category": "Diatoms"}'),
    user: dict = Depends(current_user),
):
    instrument = check_instrument(instrument)
    attrs = {}
    if metadata:
        try:
            attrs = json.loads(metadata)
            assert isinstance(attrs, dict)
        except (ValueError, AssertionError):
            raise HTTPException(400, "metadata must be a JSON object")
    file_id = store_image(file.file, file.filename, dataset, instrument, "", attrs, user["username"])
    return serialize(files_col.find_one({"_id": file_id}))


# Dropdown filters: query parameter -> stored field
FACETS = {
    "instrument": "metadata.instrument",
    "class": f"metadata.attributes.{CLASS_FIELD}",
    "status": f"metadata.attributes.{STATUS_FIELD}",
}
OTHER_FILTERS = {"dataset": "metadata.dataset", "folder": "metadata.folder", "contributor": "metadata.contributor"}


def build_query(params, user, skip_facet: Optional[str] = None) -> dict:
    q = {}
    for p, field in {**FACETS, **OTHER_FILTERS}.items():
        if p != skip_facet and params.get(p):
            v = params[p]
            q[field] = None if v == "(none)" else (v if p in OTHER_FILTERS or p == "instrument" else convert(v))
    if params.get("mine", "").lower() in ("1", "true", "yes"):
        q["metadata.uploaded_by"] = user["username"]
    for k, v in params.items():
        if k.startswith("attr."):
            q[f"metadata.attributes.{k[5:]}"] = convert(v)
    return q


@app.get("/images")
def list_images(request: Request, limit: int = 50, skip: int = 0, user: dict = Depends(current_user)):
    """Filter with ?instrument=&class=&status= (the dropdowns), ?dataset=&folder=&contributor=,
    ?mine=true (images you uploaded), and any metadata column with ?attr.<column>=<value>.
    class = species (object_annotation_category); status = object_annotation_status."""
    q = build_query(request.query_params, user)
    limit = max(1, min(limit, 500))
    cursor = files_col.find(q).sort("uploadDate", DESCENDING).skip(skip).limit(limit)
    return {"total": files_col.count_documents(q), "skip": skip, "limit": limit,
            "items": [serialize(f) for f in cursor]}


@app.get("/facets")
def facets(request: Request, user: dict = Depends(current_user)):
    """Options and counts for each dropdown. Each dropdown's counts respect the other
    dropdowns' current choices (pass the same query parameters as /images)."""
    out = {}
    for name, field in FACETS.items():
        q = build_query(request.query_params, user, skip_facet=name)
        pipeline = [{"$match": q}, {"$group": {"_id": "$" + field, "count": {"$sum": 1}}}]
        counts = {(r["_id"] if r["_id"] not in (None, "") else "(none)"): r["count"]
                  for r in files_col.aggregate(pipeline)}
        if name == "instrument":
            values = INSTRUMENTS + sorted(str(k) for k in counts if k not in INSTRUMENTS)
        else:
            values = sorted(counts, key=lambda v: (v == "(none)", str(v).lower()))
        out[name] = [{"value": v, "count": counts.get(v, 0)} for v in values]
    return out


@app.get("/instruments")
def list_instruments():
    return INSTRUMENTS


@app.get("/contributors")
def list_contributors(_: dict = Depends(current_user)):
    pipeline = [{"$group": {"_id": "$metadata.contributor", "count": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
    return [{"contributor": r["_id"], "count": r["count"]} for r in files_col.aggregate(pipeline)]


@app.get("/images/{image_id}")
def get_image_info(image_id: str, _: dict = Depends(current_user)):
    f = files_col.find_one({"_id": oid(image_id)})
    if not f:
        raise HTTPException(404, "Image not found")
    return serialize(f)


@app.get("/images/{image_id}/file")
def get_image_file(image_id: str, _: dict = Depends(current_user)):
    try:
        grid_out = fs.get(oid(image_id))
    except gridfs.errors.NoFile:
        raise HTTPException(404, "Image not found")
    meta = grid_out.metadata or {}

    def stream():
        while chunk := grid_out.read(1024 * 1024):
            yield chunk

    return StreamingResponse(
        stream(),
        media_type=meta.get("contentType") or "application/octet-stream",
        headers={"Content-Disposition": f'inline; filename="{grid_out.filename}"',
                 "Cache-Control": "private, max-age=86400"},
    )


# ---------------------------------------------------------------- annotation history

def serialize_history(h: dict) -> dict:
    return {"id": str(h["_id"]), "image_id": str(h["image_id"]), "filename": h.get("filename"),
            "changed_at": h["changed_at"], "user": h["user"], "field": h.get("field"),
            "old_value": h.get("old_value"), "new_value": h.get("new_value"), "comment": h.get("comment")}


@app.post("/images/{image_id}/annotations", status_code=201)
def annotate_image(image_id: str, status: Optional[str] = Form(None, description="New annotation status"),
                   comment: Optional[str] = Form(None, description="Optional comment"),
                   user: dict = Depends(annotator_user)):
    """Change an image's annotation status and/or add a comment. Every call is kept in its history."""
    _id = oid(image_id)
    status = (status or "").strip() or None
    comment = (comment or "").strip() or None
    if not status and not comment:
        raise HTTPException(400, "Give a new status, a comment, or both")
    if comment and len(comment) > MAX_COMMENT:
        raise HTTPException(400, f"Comment is longer than {MAX_COMMENT} characters")

    when = now()
    status_field = f"metadata.attributes.{STATUS_FIELD}"
    current = files_col.find_one({"_id": _id}, {status_field: 1, "filename": 1})
    if not current:
        raise HTTPException(404, "Image not found")
    old_status = ((current.get("metadata") or {}).get("attributes") or {}).get(STATUS_FIELD)
    if status is not None and str(status) == str(old_status):
        status = None  # unchanged: record only the comment
        if not comment:
            raise HTTPException(400, f"Status is already '{old_status}'")

    change = {"at": when, "user": user["username"]}
    update = {"$inc": {"metadata.history_count": 1}, "$set": {"metadata.last_change": change}}
    if status is not None:
        update["$set"][status_field] = status
        before = files_col.find_one_and_update({"_id": _id}, update, projection={status_field: 1},
                                               return_document=ReturnDocument.BEFORE)
        if not before:
            raise HTTPException(404, "Image not found")
        old_status = ((before.get("metadata") or {}).get("attributes") or {}).get(STATUS_FIELD)
    else:
        files_col.update_one({"_id": _id}, update)

    entry = {"image_id": _id, "filename": current.get("filename"), "changed_at": when, "user": user["username"],
             "role": user["role"],
             "field": STATUS_FIELD if status is not None else None,
             "old_value": old_status if status is not None else None,
             "new_value": status, "comment": comment}
    entry["_id"] = history.insert_one(entry).inserted_id
    return {"image": serialize(files_col.find_one({"_id": _id})), "entry": serialize_history(entry)}


@app.get("/images/{image_id}/history")
def image_history(image_id: str, _: dict = Depends(current_user)):
    """All status changes and comments for one image, newest first."""
    _id = oid(image_id)
    if not files_col.find_one({"_id": _id}, {"_id": 1}):
        raise HTTPException(404, "Image not found")
    return [serialize_history(h) for h in history.find({"image_id": _id}).sort("changed_at", DESCENDING)]


@app.get("/history")
def recent_history(user: Optional[str] = None, limit: int = 100, skip: int = 0, _: dict = Depends(current_user)):
    """Recent changes across all images, newest first. Filter with ?user=<username>."""
    q = {"user": user} if user else {}
    limit = max(1, min(limit, 1000))
    return {"total": history.count_documents(q),
            "items": [serialize_history(h) for h in history.find(q).sort("changed_at", DESCENDING).skip(skip).limit(limit)]}


@app.get("/status-choices")
def status_choices(_: dict = Depends(current_user)):
    """Statuses offered when changing an image: the standard ones plus any found in the data."""
    found = files_col.distinct(f"metadata.attributes.{STATUS_FIELD}")
    extra = sorted({str(v) for v in found if v not in (None, "")} - set(STATUS_CHOICES), key=str.lower)
    return STATUS_CHOICES + extra


@app.delete("/images/{image_id}", status_code=204)
def delete_image(image_id: str, user: dict = Depends(current_user)):
    _id = oid(image_id)
    f = files_col.find_one({"_id": _id}, {"metadata.contributor": 1, "metadata.uploaded_by": 1})
    if not f:
        raise HTTPException(404, "Image not found")
    if not can_modify(user, f):
        raise HTTPException(403, "You can only delete images you uploaded")
    fs.delete(_id)


@app.delete("/datasets/{dataset}")
def delete_dataset(dataset: str, user: dict = Depends(current_user)):
    """Deletes your own images in this dataset (an admin deletes everyone's)."""
    q = {"metadata.dataset": dataset}
    if not user.get("is_admin"):
        q["$or"] = [{"metadata.uploaded_by": user["username"]},  # older images have no uploaded_by
                    {"metadata.uploaded_by": {"$exists": False}, "metadata.contributor": user["username"]}]
    ids = [f["_id"] for f in files_col.find(q, {"_id": 1})]
    for _id in ids:
        fs.delete(_id)
    return {"dataset": dataset, "deleted": len(ids)}


@app.get("/", response_class=HTMLResponse)
def gallery():
    return GALLERY_HTML


GALLERY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Image Store</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0;background:#f6f6f4;color:#222}
 header{background:#fff;border-bottom:1px solid #ddd}
 .bar{padding:10px 20px;display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}
 .bar+.bar{border-top:1px solid #eee}
 h1{font-size:18px;margin:0 12px 0 0;align-self:center}
 label.f{display:flex;flex-direction:column;font-size:12px;color:#555;gap:3px}
 select,input,button{font:inherit;padding:6px 10px;border:1px solid #ccc;border-radius:6px;background:#fff}
 select{max-width:280px}
 button{cursor:pointer}
 .muted{font-size:14px;color:#555;align-self:center}
 #who{margin-left:auto}
 #up{display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap}
 main{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;padding:20px}
 .card{background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;flex-direction:column}
 .card img{width:100%;height:150px;object-fit:contain;display:block;background:#eee;cursor:pointer}
 .card .info{padding:6px 8px;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;flex:1}
 .card .statbtn{margin:0 8px 8px;padding:5px 6px;font-size:12px;border-color:#9bb5a3;color:#24513a;background:#f3f8f4}
 .card .statbtn:hover{background:#e3efe6}
 .pill{display:inline-block;border-radius:9px;padding:0 7px;font-size:11px;font-weight:600;background:#eceae4;color:#444}
 .pill.validated{background:#dcefe2;color:#1d5c34}
 .pill.predicted{background:#fbefd5;color:#7a5200}
 .pill.dubious{background:#f8dddd;color:#8a1f1f}
 #sdlg{width:min(460px,95vw);padding:16px 20px}
 #sdlg h3{margin:0 0 4px;font-size:16px}
 #shead{display:flex;gap:12px;align-items:center;margin-bottom:12px}
 #shead img{width:96px;height:72px;object-fit:contain;background:#eee;border-radius:6px}
 #schoices{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0 12px}
 #schoices button{flex:1 1 100px;padding:10px 8px;font-weight:600}
 #schoices button.on{outline:3px solid #2d6a4f;outline-offset:1px}
 #schoices button.current{opacity:.55;cursor:default}
 #scmt{width:100%;box-sizing:border-box;font:inherit;padding:6px 8px;border:1px solid #ccc;border-radius:6px;min-height:56px;resize:vertical}
 #smsg{font-size:13px;min-height:1em;margin-top:6px}
 #shist{list-style:none;padding:0;margin:10px 0 0;font-size:12px;color:#444}
 #shist li{border-top:1px solid #eee;padding:5px 0}
 #pager{text-align:center;padding:0 0 24px}
 dialog{width:min(1100px,95vw);border:none;border-radius:10px;padding:0}
 .dwrap{display:flex;flex-wrap:wrap;max-height:85vh;overflow:auto}
 .dleft{flex:1 1 420px;min-width:0;padding:12px}
 .dright{flex:1 1 320px;padding:12px 16px;border-left:1px solid #eee;background:#fbfbfa}
 dialog img{max-width:100%;max-height:45vh;display:block;margin:auto}
 dialog table{border-collapse:collapse;margin-top:12px;font-size:13px;width:100%}
 dialog td{border-bottom:1px solid #eee;padding:4px 8px;vertical-align:top;word-break:break-word}
 .dright h3{font-size:15px;margin:4px 0 10px}
 #annform{display:flex;flex-direction:column;gap:8px}
 #annform textarea{font:inherit;padding:6px 8px;border:1px solid #ccc;border-radius:6px;min-height:60px;resize:vertical}
 #annmsg{font-size:13px;min-height:1em}
 #hist{list-style:none;padding:0;margin:14px 0 0;font-size:13px}
 #hist li{border-top:1px solid #e6e6e2;padding:8px 0}
 #hist .when{color:#666;font-size:12px}
 #hist .chg{font-weight:600}
 #hist .cmt{margin-top:3px;white-space:pre-wrap}
 #udlg{width:min(640px,95vw);padding:16px 20px}
 #udlg h3{margin:0 0 10px;font-size:16px}
 #utable{width:100%;border-collapse:collapse;font-size:14px}
 #utable td,#utable th{padding:6px 8px;border-bottom:1px solid #eee;text-align:left}
 #uadd{display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-top:14px}
 #umsg{font-size:13px;min-height:1em;margin-top:8px}
 .note{font-size:13px;color:#555;background:#f1f1ee;border-radius:6px;padding:8px 10px}
 .badge{display:inline-block;background:#eef3ee;color:#2d5a3d;border-radius:10px;padding:0 6px;font-size:11px;margin-left:4px}
 #login{max-width:320px;margin:12vh auto;background:#fff;padding:28px;border-radius:10px;box-shadow:0 2px 10px rgba(0,0,0,.08);display:flex;flex-direction:column;gap:10px}
 #login h1{margin:0 0 6px}
 #lerr{color:#b00020;font-size:14px;min-height:1em}
 .hidden{display:none!important}
</style></head><body>
<form id="login" class="hidden">
 <h1>Image Store</h1>
 <input id="lu" placeholder="Username" autocomplete="username" required>
 <input id="lp" type="password" placeholder="Password" autocomplete="current-password" required>
 <button>Log in</button><div id="lerr"></div>
</form>
<div id="appview" class="hidden">
<header>
 <div class="bar">
  <h1>Image Store</h1>
  <form id="up">
   <label class="f">Instrument type
    <select id="upinst" required><option value="">Choose…</option></select></label>
   <label class="f">Zip file<input type="file" id="zip" accept=".zip" required></label>
   <button>Upload zip</button>
  </form>
  <span id="status" class="muted"></span>
  <span id="who" class="muted"></span>
  <button id="users" class="hidden">Users</button>
  <button id="logout">Log out</button>
 </div>
 <div class="bar">
  <label class="f">Instrument type<select id="f_instrument" data-all="All instruments"></select></label>
  <label class="f">Classes<select id="f_class" data-all="All classes"></select></label>
  <label class="f">Annotation status<select id="f_status" data-all="All statuses"></select></label>
  <label class="muted"><input type="checkbox" id="mine"> Uploaded by me</label>
  <button id="clear">Clear filters</button>
  <span id="count" class="muted"></span>
 </div>
</header>
<main id="grid"></main>
<div id="pager"><button id="more">Load more</button></div>
</div>
<dialog id="dlg"><div class="dwrap">
 <div class="dleft"><img id="big"><table id="meta"></table></div>
 <div class="dright">
  <h3>Change Annotation Status</h3>
  <div id="annnote" class="note hidden">Only annotators and admins can change the status or add comments. Ask an admin if you need annotator access.</div>
  <form id="annform">
   <label class="f"><span>Annotation status <span id="curstat"></span></span>
    <select id="newstat"></select></label>
   <label class="f">Comment (optional)<textarea id="cmt" maxlength="2000" placeholder="Why the change, or any note about this image"></textarea></label>
   <button>Save</button><div id="annmsg"></div>
  </form>
  <h3 style="margin-top:18px">History</h3>
  <ul id="hist"></ul>
 </div></div>
 <div style="display:flex;justify-content:space-between;padding:10px 12px;border-top:1px solid #eee"><button id="del" class="hidden">Delete image</button><button onclick="dlg.close()">Close</button></div></dialog>
<dialog id="sdlg">
 <div id="shead"><img id="simg"><div><h3>Change Annotation Status</h3><div id="sinfo" class="muted" style="font-size:13px"></div></div></div>
 <div id="snote" class="note hidden">Only annotators and admins can change the annotation status. Ask an admin for annotator access.</div>
 <form id="sform">
  <div style="font-size:13px">Current status: <span id="scur"></span>. Choose the new status:</div>
  <div id="schoices"></div>
  <label class="f">Comment (optional)<textarea id="scmt" maxlength="2000" placeholder="Why the change?"></textarea></label>
  <div id="smsg"></div>
  <div style="display:flex;justify-content:space-between;gap:8px;margin-top:10px">
   <button type="button" id="sfull">Full details &amp; history</button>
   <span><button type="button" onclick="sdlg.close()">Cancel</button> <button id="ssave">Save</button></span>
  </div>
 </form>
 <div id="sclose" class="hidden" style="text-align:right;margin-top:10px"><button type="button" onclick="sdlg.close()">Close</button></div>
 <ul id="shist"></ul>
</dialog>
<dialog id="udlg">
 <h3>Users</h3>
 <table id="utable"><thead><tr><th>Username</th><th>Role</th><th></th></tr></thead><tbody></tbody></table>
 <form id="uadd">
  <label class="f">New username<input id="nu" required autocomplete="off"></label>
  <label class="f">Password (8+ characters)<input id="np" type="password" minlength="8" required autocomplete="new-password"></label>
  <label class="f">Role<select id="nr"></select></label>
  <button>Add user</button>
 </form>
 <div id="umsg"></div>
 <p class="note" style="margin-top:12px"><b>Regular user:</b> browse and upload. <b>Annotator:</b> also change annotation status and add comments. <b>Admin:</b> everything, including managing users.</p>
 <div style="text-align:right;margin-top:10px"><button onclick="udlg.close()">Close</button></div>
</dialog>
<script>
const $=id=>document.getElementById(id); let skip=0, me=null, cur=null, cards={}, roles=[];
const FACETS=['instrument','class','status'];
const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(url,opt){const r=await fetch(url,opt); if(r.status===401){showLogin();throw new Error('login');} return r;}
function showLogin(){me=null;$('appview').classList.add('hidden');$('login').classList.remove('hidden');}
async function start(){
  const r=await fetch('/me'); if(!r.ok) return showLogin();
  me=await r.json(); $('login').classList.add('hidden'); $('appview').classList.remove('hidden');
  roles=await (await fetch('/roles')).json();
  const label=(roles.find(r=>r.role===me.role)||{}).label||me.role;
  $('who').textContent='Logged in as '+me.username+' ('+label+')';
  $('users').classList.toggle('hidden',!me.is_admin);
  const inst=await (await fetch('/instruments')).json();
  $('upinst').innerHTML='<option value="">Choose…</option>'+inst.map(i=>`<option>${esc(i)}</option>`).join('');
  refresh();
}
$('login').onsubmit=async e=>{
  e.preventDefault(); const fd=new FormData(); fd.append('username',$('lu').value); fd.append('password',$('lp').value);
  const r=await fetch('/login',{method:'POST',body:fd});
  if(!r.ok){$('lerr').textContent=(await r.json()).detail||'Login failed';return;}
  $('lp').value=''; $('lerr').textContent=''; start();
};
$('logout').onclick=async()=>{await fetch('/logout',{method:'POST'}); $('grid').innerHTML=''; showLogin();};
const roleSelect=(sel)=>roles.map(r=>`<option value="${r.role}"${r.role===sel?' selected':''}>${esc(r.label)}</option>`).join('');
async function loadUsers(){
  const us=await (await api('/users')).json();
  $('utable').querySelector('tbody').innerHTML=us.map(u=>`<tr><td>${esc(u.username)}</td>
    <td><select data-u="${esc(u.username)}" class="urole"${u.username===me.username?' disabled title="You can&#39;t change your own role"':''}>${roleSelect(u.role)}</select></td>
    <td style="text-align:right"><button data-pw="${esc(u.username)}">Reset password</button>
    ${u.username===me.username?'':`<button data-rm="${esc(u.username)}">Remove</button>`}</td></tr>`).join('');
  $('utable').querySelectorAll('.urole').forEach(sel=>sel.onchange=async()=>{
    const fd=new FormData(); fd.append('role',sel.value);
    const r=await api(`/users/${encodeURIComponent(sel.dataset.u)}/role`,{method:'POST',body:fd});
    $('umsg').textContent=r.ok?`${sel.dataset.u} is now ${sel.options[sel.selectedIndex].text}.`:(await r.json()).detail;
    if(!r.ok) loadUsers();
  });
  $('utable').querySelectorAll('[data-rm]').forEach(b=>b.onclick=async()=>{
    if(!confirm(`Remove ${b.dataset.rm}? Their images and history are kept.`)) return;
    const r=await api(`/users/${encodeURIComponent(b.dataset.rm)}`,{method:'DELETE'});
    $('umsg').textContent=r.ok?`${b.dataset.rm} removed.`:(await r.json()).detail; loadUsers();
  });
  $('utable').querySelectorAll('[data-pw]').forEach(b=>b.onclick=async()=>{
    const p=prompt(`New password for ${b.dataset.pw} (at least 8 characters)`); if(!p) return;
    const fd=new FormData(); fd.append('new_password',p);
    const r=await api(`/users/${encodeURIComponent(b.dataset.pw)}/password`,{method:'POST',body:fd});
    $('umsg').textContent=r.ok?`Password changed for ${b.dataset.pw}.`:(await r.json()).detail;
  });
}
$('users').onclick=()=>{$('nr').innerHTML=roleSelect('user'); $('umsg').textContent=''; loadUsers(); $('udlg').showModal();};
$('uadd').onsubmit=async e=>{
  e.preventDefault(); const fd=new FormData(); fd.append('username',$('nu').value); fd.append('password',$('np').value); fd.append('role',$('nr').value);
  const r=await api('/users',{method:'POST',body:fd}); const j=await r.json();
  $('umsg').textContent=r.ok?`Created ${j.username}.`:j.detail;
  if(r.ok){$('nu').value='';$('np').value='';$('nr').value='user';loadUsers();}
};
function filterQuery(){
  const p=new URLSearchParams();
  for(const f of FACETS){const v=$('f_'+f).value; if(v) p.set(f,v);}
  if($('mine').checked) p.set('mine','true');
  return p;
}
async function loadFilters(){
  const fc=await (await api('/facets?'+filterQuery())).json();
  for(const f of FACETS){
    const sel=$('f_'+f), v=sel.value;
    sel.innerHTML=`<option value="">${sel.dataset.all}</option>`+fc[f].map(x=>`<option value="${esc(x.value)}">${esc(x.value)} (${x.count})</option>`).join('');
    sel.value=v;
  }
}
async function load(reset){
  if(reset){skip=0;$('grid').innerHTML='';cards={};}
  const p=filterQuery(); p.set('limit',60); p.set('skip',skip);
  const r=await (await api('/images?'+p)).json();
  for(const it of r.items){
    const d=document.createElement('div'); d.className='card'; d.dataset.id=it.id;
    cards[it.id]=d; renderCard(d,it); $('grid').appendChild(d);
  }
  skip+=r.items.length; $('more').style.display=skip<r.total?'':'none';
  $('count').textContent=r.total+' image'+(r.total===1?'':'s');
}
function pill(st){const c=String(st??'').toLowerCase(); return `<span class="pill ${['validated','predicted','dubious'].includes(c)?c:''}">${esc(st??'(none)')}</span>`;}
function renderCard(d,it){
  d.innerHTML=`<img loading="lazy" src="${it.url}" alt=""><div class="info"><b>${esc(it.class??'(no class)')}</b><br>${esc(it.instrument)} · ${pill(it.status)}<br>by ${esc(it.contributor)}${it.history_count?`<span class="badge" title="changes and comments">✎ ${it.history_count}</span>`:''}</div><button class="statbtn">Change Annotation Status</button>`;
  d.querySelector('img').onclick=()=>show(it); d.querySelector('.info').onclick=()=>show(it);
  d.querySelector('.statbtn').onclick=e=>{e.stopPropagation(); openStatus(it);};
}
function updateCard(it){const d=cards[it.id]; if(d) renderCard(d,it);}
let statusChoices=null;
async function getChoices(){ if(!statusChoices) statusChoices=await (await api('/status-choices')).json(); return statusChoices; }
let scur=null, spick=null;
async function openStatus(it){
  scur=it; spick=null; $('simg').src=it.url;
  $('sinfo').innerHTML=`${esc(it.filename)}<br><b>${esc(it.class??'(no class)')}</b> · ${esc(it.instrument)}`;
  $('scur').innerHTML=pill(it.status); $('scmt').value=''; $('smsg').textContent='';
  $('sform').classList.toggle('hidden',!me.can_annotate); $('snote').classList.toggle('hidden',me.can_annotate);
  $('sclose').classList.toggle('hidden',me.can_annotate);
  const ch=await getChoices();
  $('schoices').innerHTML=ch.map(s=>{const c=String(s).toLowerCase(); const isCur=String(it.status??'')===String(s);
    return `<button type="button" data-s="${esc(s)}" class="pill ${['validated','predicted','dubious'].includes(c)?c:''}${isCur?' current':''}"${isCur?' disabled title="Current status"':''}>${esc(s)}</button>`;}).join('')
    +'<button type="button" data-s="__other" class="pill">Other…</button>';
  $('schoices').querySelectorAll('button').forEach(b=>b.onclick=()=>{
    let v=b.dataset.s; if(v==='__other'){v=(prompt('New status')||'').trim(); if(!v) return;}
    spick=v; $('schoices').querySelectorAll('button').forEach(x=>x.classList.toggle('on',x===b));
    $('smsg').textContent='New status: '+v;
  });
  $('shist').innerHTML='';
  $('sdlg').showModal();
  const h=await (await api(`/images/${it.id}/history`)).json();
  $('shist').innerHTML=h.slice(0,3).map(e=>`<li>${esc(fmtTime(e.changed_at))} · ${esc(e.user)}: `+
    (e.field?`${esc(e.old_value??'(none)')} → ${esc(e.new_value)}`:'comment')+(e.comment?` — “${esc(e.comment)}”`:'')+'</li>').join('')
    +(h.length>3?`<li>…and ${h.length-3} more (see Full details &amp; history)</li>`:'');
}
$('sform').onsubmit=async e=>{
  e.preventDefault(); const c=$('scmt').value.trim();
  if(!spick&&!c){$('smsg').textContent='Choose a new status (or write a comment).';return;}
  const fd=new FormData(); if(spick) fd.append('status',spick); if(c) fd.append('comment',c);
  const r=await api(`/images/${scur.id}/annotations`,{method:'POST',body:fd}); const j=await r.json();
  if(!r.ok){$('smsg').textContent=j.detail||'Save failed';return;}
  updateCard(j.image); loadFilters(); statusChoices=null; $('sdlg').close();
};
$('sfull').onclick=()=>{$('sdlg').close(); show(scur);};
function refresh(){loadFilters(); load(true);}
function fmtTime(t){const d=new Date(t.endsWith('Z')||t.includes('+')?t:t+'Z'); return d.toLocaleString();}
function renderMeta(it){
  const rows={filename:it.filename,instrument_type:it.instrument,dataset:it.dataset,folder:it.folder,uploaded_by:it.uploaded_by,size:it.size+' bytes',...it.attributes};
  $('meta').innerHTML=Object.entries(rows).map(([k,v])=>`<tr><td><b>${esc(k)}</b></td><td>${esc(v)}</td></tr>`).join('');
  $('curstat').textContent='(now: '+(it.status??'none')+')';
}
async function loadHistory(it){
  $('hist').innerHTML='<li class="when">Loading…</li>';
  const h=await (await api(`/images/${it.id}/history`)).json();
  $('hist').innerHTML=h.length?h.map(e=>`<li><div class="when">${esc(fmtTime(e.changed_at))} · ${esc(e.user)}</div>`+
    (e.field?`<div class="chg">Status: ${esc(e.old_value??'(none)')} → ${esc(e.new_value)}</div>`:'')+
    (e.comment?`<div class="cmt">${esc(e.comment)}</div>`:'')+'</li>').join(''):'<li class="when">No changes or comments yet.</li>';
}
async function show(it){
  cur=it; $('big').src=it.url; renderMeta(it);
  $('del').classList.toggle('hidden',!(me.is_admin||it.uploaded_by===me.username));
  $('cmt').value=''; $('annmsg').textContent='';
  $('annform').classList.toggle('hidden',!me.can_annotate); $('annnote').classList.toggle('hidden',me.can_annotate);
  const ch=await (await api('/status-choices')).json();
  $('newstat').innerHTML=`<option value="">Keep current status</option>`+ch.map(s=>`<option value="${esc(s)}">${esc(s)}</option>`).join('')+'<option value="__other">Other…</option>';
  $('dlg').showModal(); loadHistory(it);
}
$('annform').onsubmit=async e=>{
  e.preventDefault(); let st=$('newstat').value; const c=$('cmt').value.trim();
  if(st==='__other'){st=(prompt('New status')||'').trim(); if(!st) return;}
  if(!st&&!c){$('annmsg').textContent='Choose a new status or write a comment.';return;}
  const fd=new FormData(); if(st) fd.append('status',st); if(c) fd.append('comment',c);
  const r=await api(`/images/${cur.id}/annotations`,{method:'POST',body:fd}); const j=await r.json();
  if(!r.ok){$('annmsg').textContent=j.detail||'Save failed';return;}
  cur=j.image; renderMeta(cur); $('cmt').value=''; $('newstat').value=''; $('annmsg').textContent='Saved.';
  loadHistory(cur); loadFilters();
  updateCard(cur); statusChoices=null;
};
$('del').onclick=async()=>{
  if(!confirm('Delete '+cur.filename+'?')) return;
  const r=await api('/images/'+cur.id,{method:'DELETE'}); if(r.ok){$('dlg').close(); refresh();} else alert((await r.json()).detail);
};
$('up').onsubmit=async e=>{
  e.preventDefault(); const f=$('zip').files[0], inst=$('upinst').value; if(!f||!inst) return;
  const fd=new FormData(); fd.append('file',f); fd.append('instrument',inst); $('status').textContent='Uploading…';
  const r=await api('/datasets',{method:'POST',body:fd}); const j=await r.json();
  if(!r.ok){$('status').textContent=j.detail||'Upload failed';return;}
  poll(j.job_id);
};
async function poll(id){
  const j=await (await api('/jobs/'+id)).json();
  $('status').textContent=`${j.instrument||''} ${j.status}: ${j.processed||0}/${j.total??'?'} images`+(j.counts?` (${j.counts.stored} stored, ${j.counts.skipped_duplicates} duplicates)`:'');
  if(j.status==='queued'||j.status==='running') setTimeout(()=>poll(id),1500);
  else refresh();
}
for(const f of FACETS) $('f_'+f).onchange=refresh;
$('mine').onchange=refresh; $('more').onclick=()=>load(false);
$('clear').onclick=()=>{for(const f of FACETS) $('f_'+f).value=''; $('mine').checked=false; refresh();};
start();
</script></body></html>"""
