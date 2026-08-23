# -*- coding: utf-8 -*-

"""Small detached result helpers used across the component."""

import copy


def make_success(**kwargs):
    result = {"ok": True}
    result.update(copy.deepcopy(kwargs))
    return result


def make_error(code, message, **kwargs):
    result = {
        "ok": False,
        "code": str(code),
        "message": str(message),
    }
    result.update(copy.deepcopy(kwargs))
    return result
