#!/usr/bin/env python3


def deco(f):
    import time
    def wrapper():
        time.sleep(1)
        v = time.time()
        return f(v)
    return wrapper

@deco


def f1(v):
    print("f1:%s" % v)

@deco


def f2(v):
    print("f2:%s" % v)

def main():
    f1()
    f2()

main()