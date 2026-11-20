#Delay
import wave, pyaudio, threading
from tkinter import *
from tkinter import ttk, font, Canvas
import math, array

class delay():
    def __init__(self):
        self.root = Tk()
        self.root.geometry("300x200")
        self.root.title("Tremolo")
        self.root.resizable(0, 0)
        self.playing = False
        self.recording = False
        self.thread = None
        self.thread1 = None
        self.name = None
        self.fuente = font.Font(weight='bold')
        self.slider_1 = Scale(self.root,command=self.setamount,orient=HORIZONTAL, from_=100000, to=2.1, resolution=0.000001,troughcolor='light blue', showvalue=0,label="Amount")
        self.slider_1.pack()
        self.slider_1.set(100000)
        self.slider_2 = Scale(self.root,command=self.setfrecuencia,orient=HORIZONTAL, from_=1, to=8, resolution=0.000001,troughcolor='light blue', showvalue=0,label="Frequency")
        self.slider_2.pack()
        self.slider_2.set(1)
        self.slider_1.place(x=20, y=20)
        self.slider_2.place(x=150, y=20)
        self.root.mainloop()
    def setamount(self, value):
        self.valoramount = value
    def setfrecuencia(self, value):
        self.valorfreq = value

    def generadortrem(self):
        tremoloarray = array.array('h', [])
        for t in range(len(tremoloarray)):
            X = int((math.sin(t*self.valorfreq)+1)/self.valoramount)
            tremoloarray.append(X)
        return tremoloarray

def Main():
    app = delay()
    return 0

if __name__ == "__main__":
    Main()