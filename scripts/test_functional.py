# flake8: noqa
"""
関数プログラミングのテストコード
"""
import sys
import os
import time
import math

import functools
import warnings

# ---------------------------------------------------------


def simple_decorator(decorator):
    def new_decorator(f):
        # print "decorator:", f.__name__, f.__doc__, f.__dict__
        g = decorator(f)
        g.__name__ = f.__name__
        g.__doc__ = f.__doc__
        g.__dict__.update(f.__dict__)
        return g

    # print "simple_decorator: ", decorator.__name__, decorator.__doc__, decorator.__dict__
    new_decorator.__name__ = decorator.__name__
    new_decorator.__doc__ = decorator.__doc__
    new_decorator.__dict__.update(decorator.__dict__)
    return new_decorator


@simple_decorator
def my_simple_logging_decorator(func):
    def you_will_never_see_this_name(*args, **kwargs):
        print("calling {}".format(func.__name__))
        return func(*args, **kwargs)

    return you_will_never_see_this_name


@my_simple_logging_decorator
def double(x):
    "Doubles a number."
    return 2 * x


def test_simple_decorator():
    assert double.__name__ == "double", double.__name__
    assert double.__doc__ == "Doubles a number.", double.__doc__
    print(double(155))


# ---------------------------------------------------------


def my_simple_decorator2(func):
    def _decorator(*args, **kwargs):
        print("call start")
        ret = func(*args, **kwargs)
        print("call end")
        return ret

    return _decorator


def my_simple_decorator(func):
    def _decorator(x):
        print("call start")
        ret = func(x)
        print("call end")
        return ret

    print(_decorator)
    return _decorator


def my_double(x):
    print(2 * x)
    return 2 * x


def my_mul(x, y):
    print("my_mul: ", my_mul.__name__, my_mul.__doc__, my_mul.__dict__)
    return x * y


def test_my_simple_decorator():
    # my_simple_decorator(my_double)(10)
    my_simple_decorator2(my_mul)(4, 9)


# ---------------------------------------------------------


def mamoize(obj):
    cache = obj.cache = {}

    @functools.wraps
    def memoizer(*args, **kwargs):
        key = str(args) + str(kwargs)
        if key not in cache:
            cache[key] = obj(*args, **kwargs)
        return cache[key]

    return memoizer


# ---------------------------------------------------------


def retry(tries, delay=3, backoff=2):
    """
    関数を最大delay回試行しtrueなら終了するデコレータ。
    backoffは試行ごとに待ち時間を伸ばす因数
    """
    if backoff < 1:
        raise ValueError("backoff must be greater than 1")
    tries = math.floor(tries)
    if tries < 0:
        raise ValueError("tries must be 0 or greater")

    if delay <= 0:
        raise ValueError("delay must be greater than 0")

    def deco_retry(f):
        def f_retry(*args, **kwargs):
            mtries, mdelay = tries, delay  # 変更可能変数に
            rv = f(*args, **kwargs)  # 最初の試行
            while mtries > 0:
                if rv is True:
                    return True
                # 待ち
                mtries -= 1
                time.sleep(mdelay)
                mdelay *= backoff

                rv = f(*args, **kwargs)  # 再試行
            return False  # 試行回数終了

        return f_retry

    return deco_retry


# ---------------------------------------------------------
class curried(object):
    """
    カリー化 functool	s.partial
    """

    def __init__(self, func, *a):
        # print "curry_init: ", func.__name__, a
        self.func = func
        self.args = a

    def __call__(self, *a):
        # print "curry_call - arg:", a, "self_arg:", self.args
        args = self.args + a
        # print "  args:", args, self.func.func_code.co_argcount
        if len(args) < self.func.__code__.co_argcount:
            # print "call curryied"
            return curried(self.func, *args)
        else:
            # print "call func"
            return self.func(*args)


@curried
def add(a, b):
    return a + b


def test_curried():
    add1 = add(1)
    print(add1(2))


# ---------------------------------------------------------
WHAT_TO_DEBUG = set(["io", "core"])


class debug:
    """
    関数単位でのデバッグ　アスペクト指向
    """

    def __init__(self, aspects=None):
        self.aspects = set(aspects)

    def __call__(self, f):
        if self.aspects & WHAT_TO_DEBUG:

            def newf(*args, **kwds):
                print(f.__name__, args, kwds, file=sys.stderr)
                f_result = f(*args, **kwds)
                print(f.__name__, "returned", f_result, file=sys.stderr)
                return f_result

            newf.__doc__ = f.__doc__
            return newf
        else:
            return f


@debug(["io"])
def prn(x):
    print(x)


@debug(["core"])
def mult(x, y):
    return x * y


def test_debug():
    prn(mult(2, 2))


# ---------------------------------------------------------
class countcalls(object):
    "関数呼び出しの回数を保持するデコレータ"

    __instances = {}

    def __init__(self, f):
        self.__f = f
        self.__numcalls = 0
        countcalls.__instances[f] = self

    def __call__(self, *args, **kwargs):
        self.__numcalls += 1
        return self.__f(*args, **kwargs)

    @staticmethod
    def count(f):
        return countcalls.__instances[f].__numcalls

    @staticmethod
    def counts():
        return dict([(f, countcalls.count(f)) for f in countcalls.__instances])


# ---------------------------------------------------------


def deprecated(func):
    """
    関数が呼ばれると警告を発するデコレータ
    """

    @functools.wraps(func)
    def new_func(*args, **kwargs):
        # print "hoge"
        warnings.warn_explicit(
            "Call to deprecated function {}.".format(func.__name__),
            category=DeprecationWarning,
            filename=func.__code__.co_filename,
            lineno=func.__code__.co_firstlineno + 1,
        )
        return func(*args, **kwargs)

    return new_func


@deprecated
def my_func():
    pass


def test_deprecated():
    my_func()


# ---------------------------------------------------------


def unchanged(func):
    "This decorator doesn't add any behavior"
    return func


def disabled(func):
    """関数呼び出しを無効にするデコレータ"""

    def empty_func(*args, **kargs):
        pass

    return empty_func


enabled = unchanged
# ---------------------------------------------------------


def dump_args(func):
    """
    関数の引数をダンプするデコレータ
    """
    argnames = func.__code__.co_varnames[: func.__code__.co_argcount]
    fname = func.__name__

    def echo_func(*args, **kwargs):
        print(
            fname,
            ":",
            ", ".join(
                "%s=%r" % entry
                for entry in list(zip(argnames, args)) + list(kwargs.items())
            ),
        )
        return func(*args, **kwargs)

    return echo_func


@dump_args
def f1(a, b, c):
    print(a + b + c)


def test_dumpargs():
    f1(3, 4, 5)


# ---------------------------------------------------------
# ---------------------------------------------------------


def main():
    # test_simple_decorator()
    # test_my_simple_decorator()
    # test_curried()
    # test_debug()
    # test_deprecated()
    test_dumpargs()


if __name__ == "__main__":
    main()
