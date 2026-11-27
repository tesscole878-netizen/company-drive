"""Bounded search pages over the live filesystem and indexed tags."""

import base64
import binascii
import json
import os
from pathlib import Path

from .library_features import within
from .store import DriveError, valid_name

SCAN_LIMIT = 20_000


def _decode_cursor(cursor, q, tag):
    if not cursor:
        return None
    try:
        if len(cursor) > 16000:
            raise ValueError
        query, label, lid, root, path = json.loads(base64.urlsafe_b64decode(cursor))
        if (query, label) != (q, tag) or not all(isinstance(value, str) for value in (lid, root, path)):
            raise ValueError
        return lid, root, path
    except (ValueError, TypeError, binascii.Error):
        raise DriveError(400, "搜索分页位置无效，请重新搜索") from None


def _walk(folder, prefix, after):
    try:
        with os.scandir(folder) as entries:
            ordered = sorted(entries, key=lambda item: item.name)
    except (FileNotFoundError, NotADirectoryError):
        return
    for item in ordered:
        path = (*prefix, item.name)
        parent_of_cursor = after[:len(path)] == path
        if path <= after and not parent_of_cursor:
            continue
        try:
            valid_name(item.name)
            if item.is_symlink() or (os.name == 'nt' and item.is_junction()):
                continue
            is_dir = item.is_dir(follow_symlinks=False)
            if not is_dir and not item.is_file(follow_symlinks=False):
                continue
        except (DriveError, FileNotFoundError):
            continue
        if path > after:
            yield path, Path(item.path)
        if is_dir:
            yield from _walk(item.path, path, after)


def _files(store, user, after):
    roots = {}
    for lib in sorted(store.libraries_for(user), key=lambda item: (item["id"], item["root_path"])):
        lid, root = lib["id"], lib["root_path"]
        if lib["locked"] or any(within(root, parent) for parent in roots.get(lid, [])):
            continue
        roots.setdefault(lid, []).append(root)
        if after and (lid, root) < after[:2]:
            continue
        base = store.path(lid, root)
        prefix = tuple(root.split("/")) if root else ()
        position = tuple(after[2].split("/")) if after and (lid, root) == after[:2] else ()
        for path, fp in _walk(base, prefix, position):
            yield (lid, root, "/".join(path)), fp, lib


def _tagged(store, user, tag, after):
    _, catalog = store.library_grants(user)
    start_lid, _, start_path = after or ("", "", "")
    scopes = {lid: [prefix for prefix, _ in grants] for lid, (_, grants) in catalog.items()}
    # Filter before pagination so a cursor can never contain a private path.
    rows = store.all("WITH scopes AS (SELECT key lid,value paths FROM json_each(?)) "
                     "SELECT f.library_id,f.path FROM file_tags f JOIN scopes s ON s.lid=f.library_id "
                     "WHERE f.tag=? AND f.path!='' AND (f.library_id,f.path)>(?,?) AND EXISTS "
                     "(SELECT 1 FROM json_each(s.paths) p WHERE p.value='' OR f.path=p.value "
                     "OR substr(f.path,1,length(p.value)+1)=p.value||'/') "
                     "ORDER BY f.library_id,f.path LIMIT ?",
                     (json.dumps(scopes), tag, start_lid, start_path, SCAN_LIMIT + 1))
    for row in rows:
        lid, path = row["library_id"], row["path"]
        position = lid, "", path
        entry = catalog.get(lid)
        if not path or not entry or not store.role_for_path(entry[1], path):
            yield position, None, None
            continue
        lib = entry[0]
        try:
            if lib["encryption"]:
                store.library_key(user, lid)
            # Validate the physical path once when constructing a returned result.
            fp = store.files / lid / path
        except (DriveError, FileNotFoundError):
            yield position, None, None
            continue
        yield position, fp, lib


def search_files(store, user, q, tag, cursor, limit):
    q = q.strip().casefold()
    after = _decode_cursor(cursor, q, tag)
    candidates = _tagged(store, user, tag, after) if tag else _files(store, user, after)
    items, position, truncated = [], after, False
    for examined, (current, fp, lib) in enumerate(candidates):
        if examined == SCAN_LIMIT or len(items) == limit:
            truncated = True
            break
        position = current
        if fp is None or q not in fp.name.casefold():
            continue
        try:
            safe_path = store.path(current[0], current[2], True)
            if not safe_path.is_file() and not safe_path.is_dir():
                continue
            item = store.entry(current[0], safe_path, library=lib, tags=[], rel=current[2])
        except (DriveError, FileNotFoundError):
            continue
        items.append(dict(**item, library_id=current[0], library_name=lib["name"]))
    candidates.close()

    if items:
        pairs = [(item["library_id"], item["path"]) for item in items]
        values = ",".join("(?,?)" for _ in pairs)
        params = [value for pair in pairs for value in pair]
        rows = store.all(f"WITH matched(lid,path) AS (VALUES {values}) "
                         "SELECT f.library_id,f.path,f.tag FROM matched m JOIN file_tags f "
                         "ON f.library_id=m.lid AND f.path=m.path ORDER BY f.tag", params)
        tags = {}
        for row in rows:
            tags.setdefault((row["library_id"], row["path"]), []).append(row["tag"])
        for item in items:
            item["tags"] = tags.get((item["library_id"], item["path"]), [])
    next_cursor = base64.urlsafe_b64encode(json.dumps((q, tag, *position), ensure_ascii=False).encode()).decode() if truncated else None
    return {"items": items, "truncated": truncated, "next_cursor": next_cursor}
