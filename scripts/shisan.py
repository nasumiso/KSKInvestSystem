#!/usr/bin/env python3

import re

def main():
    text = open("shisan.txt","r").read()
    # pdb.set_trace()
    for m in re.finditer(r"\d\t\n\s注文\n(.+)\s円\s+([\d|,]+)", text, re.UNICODE):
        print(m.group(1),"@",m.group(2))#,m.group(3)


main()
