#!/usr/bin/env python3

import os
import time

def profile(func):
	start = time.time()
	func()
	end = time.time()
	print("time: ", end-start)

def with_profile(func):
	def _func():
		start = time.time()
		func()
		end = time.time()
		print("time: ", end-start)
	return _func;

def with_chdir(dir, func):
	def _func():
		cwd = os.getcwd()
		os.chdir(dir)
		func()
		os.chdir(cwd)
	return _func

def exe():
	print(os.getcwd())
	# print exe

def main1():
	# デコレータ
	f = with_chdir("test", exe)
	f()

def main():
	profile(main1)
	
if __name__ == '__main__':
	main()
