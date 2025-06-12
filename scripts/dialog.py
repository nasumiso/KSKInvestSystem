#!/usr/bin/env python3

class Dialog:
	def __init__(self):
		self.okPressed = None
		self.cancelPressed = None

	def show(self):
		while True:
			input = input("ボタンを押してください") # sublimeではだめ
			if input == 'ok':
				if self.okPressed:
					self.okPressed()
			elif input == 'cancel':
				if self.cancelPressed:
					self.cancelPressed()
			else:
				print("無効な入力です")

def myOkPressed():
	print("OKが押されました")


def myCancelPressed():
	print("キャンセルが押されました")

def main():
	d = Dialog()
	d.okPressed = myOkPressed
	d.cancelPressed = myCancelPressed
	d.show()

if __name__ == '__main__':
	main()
