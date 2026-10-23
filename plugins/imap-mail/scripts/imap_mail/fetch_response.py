"""Parse complete imaplib FETCH records without mixing message identities."""

from __future__ import annotations

from dataclasses import dataclass
import re


class FetchError(Exception):
    def __init__(self, code="FETCH_IDENTITY_MISMATCH"):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class FetchRecord:
    uid: str | None
    size: int | None
    flags: tuple[str, ...] | None
    internal_date: str | None
    section: str | None
    offset: int | None
    literal: bytes | None


@dataclass(frozen=True)
class _String:
    value: bytes
    literal: bool = False


def _tokens(raw):
    result, position = [], 0
    while position < len(raw):
        char = raw[position:position + 1]
        if char == b" ":
            position += 1
        elif char in (b"(", b")"):
            result.append(char)
            position += 1
        elif char == b'"':
            position += 1
            value = bytearray()
            while position < len(raw) and raw[position:position + 1] != b'"':
                if raw[position:position + 1] == b"\\":
                    position += 1
                    if position >= len(raw) or raw[position:position + 1] not in (b"\\", b'"'):
                        raise FetchError()
                value.append(raw[position])
                position += 1
            if position >= len(raw):
                raise FetchError()
            result.append(_String(bytes(value)))
            position += 1
        else:
            start = position
            while position < len(raw) and raw[position:position + 1] not in (b" ", b"(", b")"):
                if raw[position:position + 1] == b"[" and raw[start:position].upper() == b"BODY":
                    end = raw.find(b"]", position)
                    if end < 0:
                        raise FetchError()
                    position = end
                if raw[position] < 32 or raw[position] > 126 or raw[position:position + 1] in (b'"', b"{", b"}"):
                    raise FetchError()
                position += 1
            result.append(raw[start:position])
    return result


def _value(tokens, index):
    if index >= len(tokens) or tokens[index] == b")":
        raise FetchError()
    value = tokens[index]
    if value != b"(":
        return value, index + 1
    result, index = [], index + 1
    while index < len(tokens) and tokens[index] != b")":
        value, index = _value(tokens, index)
        result.append(value)
    if index >= len(tokens):
        raise FetchError()
    return result, index + 1


def _number(value, nonzero=False):
    if not isinstance(value, bytes) or not re.fullmatch(rb"[0-9]+", value):
        raise FetchError()
    number = int(value)
    if number > 4294967295 or (nonzero and number == 0):
        raise FetchError()
    return number


def fetch_records(rows):
    if not isinstance(rows, list):
        raise FetchError()
    tokens = []
    for row in rows:
        if row is None and len(rows) == 1:
            continue
        if isinstance(row, tuple) and len(row) == 2:
            prefix, payload = row
            if not isinstance(prefix, bytes) or not isinstance(payload, bytes):
                raise FetchError()
            declaration = re.search(rb"\{([0-9]+)\}$", prefix)
            if not declaration or int(declaration[1]) != len(payload):
                raise FetchError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
            tokens.extend(_tokens(prefix[:declaration.start()]))
            tokens.append(_String(payload, literal=True))
        elif isinstance(row, bytes):
            tokens.extend(_tokens(row))
        else:
            raise FetchError()
    records, index = [], 0
    while index < len(tokens):
        _number(tokens[index], nonzero=True)
        attributes, index = _value(tokens, index + 1)
        if not isinstance(attributes, list) or len(attributes) % 2:
            raise FetchError()
        fields = {}
        for name, value in zip(attributes[::2], attributes[1::2]):
            if not isinstance(name, bytes) or not re.fullmatch(rb"[A-Za-z][A-Za-z0-9.\[\] ()<>-]*", name):
                raise FetchError()
            name = name.upper()
            if name in fields:
                raise FetchError()
            fields[name] = value
        uid = str(_number(fields[b"UID"], nonzero=True)) if b"UID" in fields else None
        size = _number(fields[b"RFC822.SIZE"]) if b"RFC822.SIZE" in fields else None
        flags = fields.get(b"FLAGS")
        if flags is not None:
            if not isinstance(flags, list) or any(not isinstance(flag, bytes) for flag in flags):
                raise FetchError()
            flags = tuple(flag.decode("ascii") for flag in flags)
        internal = fields.get(b"INTERNALDATE")
        if internal is not None and not isinstance(internal, _String):
            raise FetchError()
        internal = internal.value.decode("ascii") if internal is not None else None
        bodies = [(name, value) for name, value in fields.items()
                  if name.startswith((b"BODY[", b"BODY.PEEK["))]
        if len(bodies) > 1:
            raise FetchError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
        section = offset = literal = None
        if bodies:
            name, value = bodies[0]
            body = re.fullmatch(rb"BODY\[(.*)\](?:<([0-9]+)>)?", name)
            if not body or not isinstance(value, _String) or (not value.literal and value.value):
                raise FetchError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
            section = body[1].decode("ascii")
            offset = _number(body[2]) if body[2] is not None else None
            literal = value.value
        records.append(FetchRecord(uid, size, flags, internal, section, offset, literal))
    return tuple(records)


def requested_fetch(records, uid, section, offset=None):
    bodies = [record for record in records if record.literal is not None]
    matches = [record for record in bodies if record.uid == str(uid)]
    if not bodies:
        raise FetchError("MESSAGE_UNAVAILABLE")
    if len(matches) != 1:
        raise FetchError()
    record = matches[0]
    if record.section != section.upper() or record.offset != offset:
        raise FetchError("MESSAGE_TOO_LARGE_OR_INCOMPLETE")
    return record
