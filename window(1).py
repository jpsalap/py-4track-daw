from tkinter import *
from tkinter import ttk, font, Canvas, PhotoImage, messagebox
from tkinter.filedialog import askopenfilename
from Delay import delay
from Tremolo import tremolo
from Chorus import chorus
from Compressor import compressor
import array, wave, pyaudio, threading

def greater_array(A, B, C, D):
    a = len(A)
    b = len(B)
    c = len(C)
    d = len(D)
    e = [a, b, c, d]
    greater = e[0]
    iGreat = 0
    for i in range(len(e)):
        if e[i] > greater:
            greater = e[i]
            iGreat = i
    if iGreat == 0:
        Greater_Array = A
    elif iGreat == 1:
        Greater_Array = B
    elif iGreat == 2:
        Greater_Array = C
    else:
        Greater_Array = D
    return Greater_Array

def extend_array(A, Greater_Array):
    Zero_array = array.array('h', [0])
    A.extend(Zero_array*(len(Greater_Array)-len(A)))
    return A

def adjust_vol(data, vol):
    Vol = array.array('h', data)
    for i in range(len(Vol)):
        Vol[i] = int(Vol[i]*float(vol))
    return Vol

def adjust_pan(A, Pan):
    Stereo = array.array('h', [])
    Left = array.array('h', [])
    Right = array.array('h', [])
    for i in range(0, len(A), 1):
        Right.append(int(A[i]*float(Pan)))
        Left.append(int(A[i]*(1-float(Pan))))
    for i in range(len(A)):
        Stereo.append(Left[i])
        Stereo.append(Right[i])
    return Stereo

def audio_sum(A, B):
    C = array.array('h', [])
    for i in range(len(A)):
        if A[i]+B[i] <= (-2**16/2):
            C.append(int(1-(2**16/2)))
        elif A[i]+B[i] >= (2**16/2):
            C.append(int(2**16/2)-1)
        else:
            C.append(A[i]+B[i])
    return C

def rewind(A, B, C, D):
    A.rewind()
    B.rewind()
    C.rewind()
    D.rewind()

class Window():
    #to initialize the window
    def __init__(self):
        #se crean las variables de estado playing y recording
        self.playing = False
        self.bouncing = False
        #creacion variables vacias que posteriormente seran llenadas por valores o serán instancias
        self.thread = None
        self.archive1 = None
        self.archive2 = None
        self.archive3 = None
        self.archive4 = None
        self.name = None
        self.bounce_name = None
        self.buffer = 1024 #definicion del chunk o buffer con el que se reproduce y graba
        self.bitdepth = 16 #definicion de la produnfidad en bits para grabar audio
        self.channels = 2 #numero de canales, define si se graba en mono o stereo
        self.rate = 44100#frecuencia de muestreo del audio a grabar
        self.root = Tk() #creacion de la ventana
        self.root.geometry("880x500") #dimensiones de la ventana
        self.root.title("Audio Player and Mixer") #nombre superior de la ventana
        self.root.resizable(0, 0)  #no permite reescalado de la ventana
        self.root.configure(bg='steel blue') #define el color de fondo de la ventana
        self.font_ = font.Font(weight='bold')  # define el tipo de fuente
        self.menu = Menu(self.root)  # creacion de un menu
        self.root.config(menu=self.menu)  # se añade el menu a la ventana
        self.file_menu = Menu(self.menu)
        self.menu.add_cascade(label="File", menu=self.file_menu)  # añade la opción de desplegar opciones
        # añade las opciones del menu
        self.file_menu.add_command(label="Exit", command=self.root.quit)  # opcion salir
        #definen variables con valores variables de tkinter
        self.nameEntry = StringVar()
        self.volume1_value = 1
        self.volume2_value = 1
        self.volume3_value = 1
        self.volume4_value = 1
        self.pan1_value = 0.5
        self.pan2_value = 0.5
        self.pan3_value = 0.5
        self.pan4_value = 0.5
        #checkbutton en la ventana para marcar loop y direct monitoring
        #Entry para el nombre del archivo a guardar
        self.nameBox = ttk.Entry(self.root, textvariable=self.nameEntry, width=10)
        #checkbuttons para fx
        self.delay1_value = BooleanVar()
        self.delay2_value = BooleanVar()
        self.delay3_value = BooleanVar()
        self.delay4_value = BooleanVar()
        self.tremolo1_value = BooleanVar()
        self.tremolo2_value = BooleanVar()
        self.tremolo3_value = BooleanVar()
        self.tremolo4_value = BooleanVar()
        self.chorus1_value = BooleanVar()
        self.chorus2_value = BooleanVar()
        self.chorus3_value = BooleanVar()
        self.chorus4_value = BooleanVar()
        self.compressor1_value = BooleanVar()
        self.compressor2_value = BooleanVar()
        self.compressor3_value = BooleanVar()
        self.compressor4_value = BooleanVar()
        self.delay1 = Checkbutton(self.root, text="Delay", variable=self.delay1_value, background="blue", font=("Montserrat", 10), fg="white", command=self.delay)
        self.delay2 = Checkbutton(self.root, text="Delay", variable=self.delay2_value, background="blue", font=("Montserrat", 10), fg="white", command=self.delay)
        self.delay3 = Checkbutton(self.root, text="Delay", variable=self.delay3_value, background="blue", font=("Montserrat", 10), fg="white", command=self.delay)
        self.delay4 = Checkbutton(self.root, text="Delay", variable=self.delay4_value, background="blue", font=("Montserrat", 10), fg="white", command=self.delay)
        self.tremolo1 = Checkbutton(self.root, text="Tremolo", variable=self.tremolo1_value, background="forest green", font=("Montserrat", 10), fg="white", command=self.tremolo)
        self.tremolo2 = Checkbutton(self.root, text="Tremolo", variable=self.tremolo2_value, background="forest green", font=("Montserrat", 10), fg="white", command=self.tremolo)
        self.tremolo3 = Checkbutton(self.root, text="Tremolo", variable=self.tremolo3_value, background="forest green", font=("Montserrat", 10), fg="white", command=self.tremolo)
        self.tremolo4 = Checkbutton(self.root, text="Tremolo", variable=self.tremolo4_value, background="forest green", font=("Montserrat", 10), fg="white", command=self.tremolo)
        self.chorus1 = Checkbutton(self.root, text="Chorus", variable=self.chorus1_value, background="turquoise3", font=("Montserrat", 10), fg="white", command=self.chorus)
        self.chorus2 = Checkbutton(self.root, text="Chorus", variable=self.chorus2_value, background="turquoise3", font=("Montserrat", 10), fg="white", command=self.chorus)
        self.chorus3 = Checkbutton(self.root, text="Chorus", variable=self.chorus3_value, background="turquoise3", font=("Montserrat", 10), fg="white", command=self.chorus)
        self.chorus4 = Checkbutton(self.root, text="Chorus", variable=self.chorus4_value, background="turquoise3", font=("Montserrat", 10), fg="white", command=self.chorus)
        self.compressor1 = Checkbutton(self.root, text="Compressor", variable=self.compressor1_value, background="firebrick3", font=("Montserrat", 10), fg="white", command=self.compressor)
        self.compressor2 = Checkbutton(self.root, text="Compressor", variable=self.compressor2_value, background="firebrick3", font=("Montserrat", 10), fg="white", command=self.compressor)
        self.compressor3 = Checkbutton(self.root, text="Compressor", variable=self.compressor3_value, background="firebrick3", font=("Montserrat", 10), fg="white", command=self.compressor)
        self.compressor4 = Checkbutton(self.root, text="Compressor", variable=self.compressor4_value, background="firebrick3", font=("Montserrat", 10), fg="white", command=self.compressor)
        #slider que controlan la ganancia y el volumen
        self.volume1 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=VERTICAL, length=200, command=self.set_vol1, bg="navy", troughcolor="RoyalBlue4")
        self.volume1.set(0.5)
        self.volume1.pack()
        self.volume2 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=VERTICAL, length=200, command=self.set_vol2, bg="navy", troughcolor="RoyalBlue4")
        self.volume2.set(0.5)
        self.volume2.pack()
        self.volume3 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=VERTICAL, length=200, command=self.set_vol3, bg="navy", troughcolor="RoyalBlue4")
        self.volume3.set(0.5)
        self.volume3.pack()
        self.volume4 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=VERTICAL, length=200, command=self.set_vol4, bg="navy", troughcolor="RoyalBlue4")
        self.volume4.set(0.5)
        self.volume4.pack()
        self.pan1 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=HORIZONTAL, command=self.set_pan1, bg="dark slate gray", troughcolor="DarkSlateGray4")
        self.pan1.set(0.5)
        self.pan1.pack()
        self.pan2 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=HORIZONTAL, command=self.set_pan2, bg="dark slate gray", troughcolor="DarkSlateGray4")
        self.pan2.set(0.5)
        self.pan2.pack()
        self.pan3 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=HORIZONTAL, command=self.set_pan3, bg="dark slate gray", troughcolor="DarkSlateGray4")
        self.pan3.set(0.5)
        self.pan3.pack()
        self.pan4 = Scale(self.root, from_=1, to=0, resolution=0.01, showvalue=0, orient=HORIZONTAL, command=self.set_pan4, bg="dark slate gray", troughcolor="DarkSlateGray4")
        self.pan4.set(0.5)
        self.pan4.pack()
        #labels para señalizar en la ventana
        self.ch1_label = ttk.Label(self.root, text="Channel 1", font=self.font_)
        self.ch2_label = ttk.Label(self.root, text="Channel 2", font=self.font_)
        self.ch3_label = ttk.Label(self.root, text="Channel 3", font=self.font_)
        self.ch4_label = ttk.Label(self.root, text="Channel 4", font=self.font_)
        self.name_label = ttk.Label(self.root, text="Name: ", font=self.font_)
        #se cargan imagenes que están en el directorio del proyecto
        self.img1 = PhotoImage(file="play.png")
        self.img2 = PhotoImage(file="stop.png")
        self.img3 = PhotoImage(file="rec.png")
        self.img4 = PhotoImage(file="open.png")
        #se escalan las imagenes a un tamaño apropiado a la ventana
        self.img_play = self.img1
        self.img_stop = self.img2
        self.img_rec = self.img3
        self.img_open = self.img4
        #se crean tres botones para elegir la ubicacion del archivo, activar la reproduccion del archivo y parar la reproduccion
        self.play_button = ttk.Button(self.root, command=self.press_play)
        self.stop_button = ttk.Button(self.root,  command=self.stop)
        self.rec_button = ttk.Button(self.root, command=self.rec)
        self.export_button = Button(self.root, command=self.bounce, text="Bounce", highlightbackground="olive drab")
        self.open_button1 = ttk.Button(self.root, command=self.open1)
        self.open_button2 = ttk.Button(self.root, command=self.open2)
        self.open_button3 = ttk.Button(self.root, command=self.open3)
        self.open_button4 = ttk.Button(self.root, command=self.open4)
        #se colocan las imagenes cargada dentro de los botones
        self.play_button.config(image=self.img_play)
        self.stop_button.config(image=self.img_stop)
        self.rec_button.config(image=self.img_rec)
        self.open_button1.config(image=self.img_open)
        self.open_button2.config(image=self.img_open)
        self.open_button3.config(image=self.img_open)
        self.open_button4.config(image=self.img_open)
        #checkbutton channel options
        self.solo1_value = BooleanVar()
        self.solo2_value = BooleanVar()
        self.solo3_value = BooleanVar()
        self.solo4_value = BooleanVar()
        self.rec1_value = BooleanVar()
        self.rec2_value = BooleanVar()
        self.rec3_value = BooleanVar()
        self.rec4_value = BooleanVar()
        self.mute1_value = BooleanVar()
        self.mute2_value = BooleanVar()
        self.mute3_value = BooleanVar()
        self.mute4_value = BooleanVar()
        self.solo1 = Checkbutton(self.root, text="Solo", variable=self.solo1_value, background="seashell3", font=("Montserrat", 10), fg="white")
        self.solo2 = Checkbutton(self.root, text="Solo", variable=self.solo2_value, background="seashell3", font=("Montserrat", 10), fg="white")
        self.solo3 = Checkbutton(self.root, text="Solo", variable=self.solo3_value, background="seashell3", font=("Montserrat", 10), fg="white")
        self.solo4 = Checkbutton(self.root, text="Solo", variable=self.solo4_value, background="seashell3", font=("Montserrat", 10), fg="white")
        self.rec1 = Checkbutton(self.root, text="Rec", variable=self.rec1_value, background="brown4", font=("Montserrat", 10), fg="white")
        self.rec2 = Checkbutton(self.root, text="Rec", variable=self.rec2_value, background="brown4", font=("Montserrat", 10), fg="white")
        self.rec3 = Checkbutton(self.root, text="Rec", variable=self.rec3_value, background="brown4", font=("Montserrat", 10), fg="white")
        self.rec4 = Checkbutton(self.root, text="Rec", variable=self.rec4_value, background="brown4", font=("Montserrat", 10), fg="white")
        self.mute1 = Checkbutton(self.root, text="Mute", variable=self.mute1_value, background="DarkGoldenrod1", font=("Montserrat", 10), fg="white")
        self.mute2 = Checkbutton(self.root, text="Mute", variable=self.mute2_value, background="DarkGoldenrod1", font=("Montserrat", 10), fg="white")
        self.mute3 = Checkbutton(self.root, text="Mute", variable=self.mute3_value, background="DarkGoldenrod1", font=("Montserrat", 10), fg="white")
        self.mute4 = Checkbutton(self.root, text="Mute", variable=self.mute4_value, background="DarkGoldenrod1", font=("Montserrat", 10), fg="white")
        #posiciona checkbuttons, labels y los botones segun coordenadas x e y
        #channel 1
        self.delay1.place(x=40, y=10)
        self.tremolo1.place(x=40, y=30)
        self.chorus1.place(x=40, y=50)
        self.compressor1.place(x=40, y=70)
        self.ch1_label.place(x=40, y=90)
        self.pan1.place(x=23, y=125)
        self.volume1.place(x=65, y=160)
        self.solo1.place(x=50, y=420)
        self.rec1.place(x=50, y=440)
        self.mute1.place(x=50, y=460)
        #channel 2
        self.delay2.place(x=190, y=10)
        self.tremolo2.place(x=190, y=30)
        self.chorus2.place(x=190, y=50)
        self.compressor2.place(x=190, y=70)
        self.ch2_label.place(x=190, y=90)
        self.pan2.place(x=173, y=125)
        self.volume2.place(x=215, y=160)
        self.solo2.place(x=205, y=420)
        self.rec2.place(x=205, y=440)
        self.mute2.place(x=205, y=460)
        #channel 3
        self.delay3.place(x=340, y=10)
        self.tremolo3.place(x=340, y=30)
        self.chorus3.place(x=340, y=50)
        self.compressor3.place(x=340, y=70)
        self.ch3_label.place(x=340, y=90)
        self.pan3.place(x=323, y=125)
        self.volume3.place(x=365, y=160)
        self.solo3.place(x=355, y=420)
        self.rec3.place(x=355, y=440)
        self.mute3.place(x=355, y=460)
        #channel 4
        self.delay4.place(x=490, y=10)
        self.tremolo4.place(x=490, y=30)
        self.chorus4.place(x=490, y=50)
        self.compressor4.place(x=490, y=70)
        self.ch4_label.place(x=490, y=90)
        self.pan4.place(x=473, y=125)
        self.volume4.place(x=515, y=160)
        self.solo4.place(x=505, y=420)
        self.rec4.place(x=505, y=440)
        self.mute4.place(x=505, y=460)
        #name
        self.name_label.place(x=680, y=20)
        self.nameBox.place(x=680, y=45)
        #buttons
        self.play_button.place(x=710, y=350)
        self.stop_button.place(x=630, y=350)
        self.rec_button.place(x=790, y=350)
        self.export_button.place(x=800, y=25)
        self.open_button1.place(x=40, y=380)
        self.open_button2.place(x=195, y=380)
        self.open_button3.place(x=345, y=380)
        self.open_button4.place(x=495, y=380)
        #mantiene la ventana en ejecución hasta que es cerrada
        self.root.mainloop()
    def delay(self):
        if self.delay1_value.get() or self.delay2_value.get() or self.delay3_value.get() or self.delay4_value.get():
            self.delay_win = Toplevel(self.root)
            self.delay_win.title("Delay")
            self.delay_win.transient(self.root)
            Delay = delay(self.delay_win)
    def tremolo(self):
        if self.tremolo1_value.get() or self.tremolo2_value.get() or self.tremolo3_value.get() or self.tremolo4_value.get():
            self.tremolo_win = Toplevel(self.root)
            self.tremolo_win.title("Tremolo")
            self.delay_win.transient(self.root)
            Tremolo = tremolo(self.tremolo_win)
    def chorus(self):
        if self.chorus1_value.get() or self.chorus2_value.get() or self.chorus3_value.get() or self.chorus4_value.get():
            self.chorus_win = Toplevel(self.root)
            self.chorus_win.title("Chorus")
            self.delay_win.transient(self.root)
            Chorus = chorus(self.chorus_win)
    def compressor(self):
        if self.compressor1_value.get() or self.compressor2_value.get() or self.compressor3_value.get() or self.compressor4_value.get():
            self.compressor_win = Toplevel(self.root)
            self.compressor_win.title("Compressor")
            self.delay_win.transient(self.root)
            Compressor = compressor(self.compressor_win)
    #definicion del metodo open para buscar la ruta del archivo
    def open1(self):
        self.archive1 = askopenfilename(filetypes=(("wav files", "*.wav"),("All files", "*.*")))# abre el cuadro de dialogo que permite seleccionar la ruta del archivo permitiendo solo archivos .wav
        Tk().destroy() # destruye el cuadro de dialogo abierto para seleccionar la ruta
        self.audio1 = wave.open(self.archive1, 'rb')
        channels = self.audio1.getnchannels()  # obtiene el numero de canales del archivo
        samplefreq = (self.audio1.getframerate())  # obtiene la frecuencia de muestreo
        bitdepth = self.audio1.getsampwidth() * 8  # obtiene la profundidad en bits, tomandolo en bytes y convirtiendo a bits
        length = int(self.audio1.getnframes() / samplefreq)  # obtiene la duracion del archivo de audio en segundos a partir del numero de cuadros y la frecuencia de muestreo
        for i in range(60):  # ciclo que convierte la duracion en segundos a minutos y segundos
            min = int(length / 60)  # se toman como entero los minutos que dura el archivo
            sec = length % 60  # el resto (modulo) de la division serán los segundos
        # se muestre por consola la ruta del archivo, el numero de canales, la frecuencia de muestreo, la profundidad en bits y la duración del archivo
        print('Channel 1 audio loaded'
              '\n File route: ', self.name,
              '\n Number of channels: ', channels,
              '\n Sample rate: ', samplefreq,
              '\n Bit Depth: ', bitdepth,
              '\n Duration:', min, ':', sec)
        self.audio1.close()
    def open2(self):
        self.archive2 = askopenfilename(filetypes=(("wav files", "*.wav"), ("All files", "*.*")))  # abre el cuadro de dialogo que permite seleccionar la ruta del archivo permitiendo solo archivos .wav
        Tk().destroy()  # destruye el cuadro de dialogo abierto para seleccionar la ruta
        self.audio2 = wave.open(self.archive2, 'rb')  # objeto archive del tipo wave.open que abre un archivo wav en concreto en modo lectura
        channels = self.audio2.getnchannels()  # obtiene el numero de canales del archivo
        samplefreq = self.audio2.getframerate()  # obtiene la frecuencia de muestreo
        bitdepth = self.audio2.getsampwidth() * 8  # obtiene la profundidad en bits, tomandolo en bytes y convirtiendo a bits
        length = int(
            self.audio2.getnframes() / samplefreq)  # obtiene la duracion del archivo de audio en segundos a partir del numero de cuadros y la frecuencia de muestreo
        for i in range(60):  # ciclo que convierte la duracion en segundos a minutos y segundos
            min = int(length / 60)  # se toman como entero los minutos que dura el archivo
            sec = length % 60  # el resto (modulo) de la division serán los segundos
        # se muestre por consola la ruta del archivo, el numero de canales, la frecuencia de muestreo, la profundidad en bits y la duración del archivo
        print('Channel 2 audio loaded'
              '\n File route: ', self.name,
              '\n Number of channels: ', channels,
              '\n Sample rate: ', samplefreq,
              '\n Bit Depth: ', bitdepth,
              '\n Duration:', min, ':', sec)
        self.audio2.close()
    def open3(self):
        self.archive3 = askopenfilename(filetypes=(("wav files", "*.wav"), ("All files", "*.*")))  # abre el cuadro de dialogo que permite seleccionar la ruta del archivo permitiendo solo archivos .wav
        Tk().destroy()  # destruye el cuadro de dialogo abierto para seleccionar la ruta
        self.audio3 = wave.open(self.archive3, 'rb')  # objeto archive del tipo wave.open que abre un archivo wav en concreto en modo lectura
        channels = self.audio3.getnchannels()  # obtiene el numero de canales del archivo
        samplefreq = self.audio3.getframerate()  # obtiene la frecuencia de muestreo
        bitdepth = self.audio3.getsampwidth() * 8  # obtiene la profundidad en bits, tomandolo en bytes y convirtiendo a bits
        length = int(
            self.audio3.getnframes() / samplefreq)  # obtiene la duracion del archivo de audio en segundos a partir del numero de cuadros y la frecuencia de muestreo
        for i in range(60):  # ciclo que convierte la duracion en segundos a minutos y segundos
            min = int(length / 60)  # se toman como entero los minutos que dura el archivo
            sec = length % 60  # el resto (modulo) de la division serán los segundos
        # se muestre por consola la ruta del archivo, el numero de canales, la frecuencia de muestreo, la profundidad en bits y la duración del archivo
        print('Channel 3 audio loaded'
              '\n File route: ', self.name,
              '\n Number of channels: ', channels,
              '\n Sample rate: ', samplefreq,
              '\n Bit Depth: ', bitdepth,
              '\n Duration:', min, ':', sec)
        self.audio3.close()
    def open4(self):
        self.archive4 = askopenfilename(filetypes=(("wav files", "*.wav"), ("All files", "*.*")))  # abre el cuadro de dialogo que permite seleccionar la ruta del archivo permitiendo solo archivos .wav
        Tk().destroy()  # destruye el cuadro de dialogo abierto para seleccionar la ruta
        self.audio4 = wave.open(self.archive4, 'rb')  # objeto archive del tipo wave.open que abre un archivo wav en concreto en modo lectura
        channels = self.audio4.getnchannels()  # obtiene el numero de canales del archivo
        samplefreq = self.audio4.getframerate() # obtiene la frecuencia de muestreo
        bitdepth = self.audio4.getsampwidth() * 8  # obtiene la profundidad en bits, tomandolo en bytes y convirtiendo a bits
        length = int(self.audio4.getnframes() / samplefreq)  # obtiene la duracion del archivo de audio en segundos a partir del numero de cuadros y la frecuencia de muestreo
        for i in range(60):  # ciclo que convierte la duracion en segundos a minutos y segundos
            min = int(length / 60)  # se toman como entero los minutos que dura el archivo
            sec = length % 60 # el resto (modulo) de la division serán los segundos
        # se muestre por consola la ruta del archivo, el numero de canales, la frecuencia de muestreo, la profundidad en bits y la duración del archivo
        print('Channel 4 audio loaded '
              '\n File route: ', self.name,
              '\n Number of channels: ', channels,
              '\n Sample rate: ', samplefreq,
              '\n Bit Depth: ', bitdepth,
              '\n Duration:', min, ':', sec)
        self.audio4.close()
    #definicion del metodo play para reproducir el archivo seleccionado
    def play(self): #terminar metodo play para poder reproducir la suma de los 4 archivos
        print("Now playing...")
        audio = pyaudio.PyAudio()  # se crea un objeto audio del tipo pyaudio

        audio1 = wave.open(self.archive1, 'rb')
        audio2 = wave.open(self.archive2, 'rb')
        audio3 = wave.open(self.archive3, 'rb')
        audio4 = wave.open(self.archive4, 'rb')

        # se crea un objeto stream que permitira que flujo de informacion del archivo
        stream = audio.open(format=audio.get_format_from_width(audio1.getsampwidth()),  # obtiene la profundidad de bits
                            channels=audio1.getnchannels(),  # se obtiene el numero de canales
                            rate=88200,  # se obtiene la frecuencia de muestreo
                            output=True)  # se define que está reproduciendo

        A = audio1.readframes(self.buffer)
        B = audio2.readframes(self.buffer)
        C = audio3.readframes(self.buffer)
        D = audio4.readframes(self.buffer)

        while self.playing:  # ciclo se ejecuta mientras la variable data no esté sin valores y el estado playing sea verdadero

            A = adjust_vol(A, self.volume1_value)
            B = adjust_vol(B, self.volume2_value)
            C = adjust_vol(C, self.volume3_value)
            D = adjust_vol(D, self.volume4_value)

            Greater_Array = greater_array(A, B, C, D)

            if len(A) != len(Greater_Array):
                A = extend_array(A, Greater_Array)
            if len(B) != len(Greater_Array):
                B = extend_array(B, Greater_Array)
            if len(C) != len(Greater_Array):
                C = extend_array(C, Greater_Array)
            if len(D) != len(Greater_Array):
                D = extend_array(D, Greater_Array)



            A = adjust_pan(A, self.pan1_value)
            B = adjust_pan(B, self.pan2_value)
            C = adjust_pan(C, self.pan3_value)
            D = adjust_pan(D, self.pan4_value)

            AB = audio_sum(A, B)
            CD = audio_sum(C, D)
            E = audio_sum(AB, CD)


            data = array.array.tostring(E) #el arreglo modificado en su volumen se convierte a string para ser leido y reproducido
            stream.write(data)  # escribe los cuadros por segundo leidos en el stream para reproducirlos
            A = audio1.readframes(self.buffer)
            B = audio2.readframes(self.buffer)
            C = audio3.readframes(self.buffer)
            D = audio4.readframes(self.buffer)  # vuelve a leer los cuadros por segundo segun el tamaño del chunk
            if data == b'':  # si data se queda sin datos para leer termina la reproduccion
                self.playing = False
                print("Audio ended \n")  # se imprime un mensaje de que el audio a terminado
        stream.stop_stream()  # se detiene el flujo de información
        stream.close()  # cierra el flujo de info.
        audio.terminate()  # termina el objeto audio de pyaudio para limpiar memoria
    #definicion del metodo press_play que determina el estado del programa(reproduciendo o no)
    def press_play(self):
        if not self.archive1 and not self.archive2 and not self.archive3 and not self.archive4: # si no se ha seleccionado un archivo se manda un mensaje
            messagebox._show(message="There's no audio file opened to play. Please choose .wav audio files")
        else: # al estar seleccionado un archivo se procede a reproducir
            if not self.playing: # si no está reproduciendo procede a reproducir, y si esta reproduciendo continua con la reproduccion actual
                self.playing = True # el estado de programa se vuelve reproduciendo
                self.thread = threading.Thread(target=self.play) # se crea sobre thread un objeto thread( un hilo) de ejecución donde se ejecuta el metodo play
                self.thread.start() # inicia el hilo de ejecución creado
    #definción del metodo stop que detiene la reproduccion del archivo
    def stop(self):
        if self.playing: # solo puede detener la reproducción si el estado del programa es reproduciendo
            self.playing = False # el estado de programa ya no es reproduciendo
            print("Stopped")
            self.thread.join() # espera a que el hilo en ejecucion se termine
    #definicion de metodos set volume que toma el valor del fader, retorna un float
    def set_vol1(self,v1_slider):
        self.volume1_value = v1_slider
    def set_vol2(self,v2_slider):
        self.volume2_value = v2_slider
    def set_vol3(self,v3_slider):
        self.volume3_value = v3_slider
    def set_vol4(self,v4_slider):
        self.volume4_value = v4_slider
    # definicion del metodos set pan que toma el valor del fader, retorna un float
    def set_pan1(self,p1_slider):
        self.pan1_value = p1_slider
    def set_pan2(self,p2_slider):
        self.pan1_value = p2_slider
    def set_pan3(self,p3_slider):
        self.pan1_value = p3_slider
    def set_pan4(self,p4_slider):
        self.pan1_value = p4_slider
    def rec(self):
        pass
    def bounce(self):
        print("Bouncing audios...")
        audio1 = wave.open(self.archive1, 'rb')
        audio2 = wave.open(self.archive2, 'rb')
        audio3 = wave.open(self.archive3, 'rb')
        audio4 = wave.open(self.archive4, 'rb')

        A = audio1.readframes(audio1.getnframes())
        B = audio2.readframes(audio2.getnframes())
        C = audio3.readframes(audio3.getnframes())
        D = audio4.readframes(audio4.getnframes())

        A = adjust_vol(A, self.volume1_value)
        B = adjust_vol(B, self.volume2_value)
        C = adjust_vol(C, self.volume3_value)
        D = adjust_vol(D, self.volume4_value)

        GreaterArray = greater_array(A, B, C, D)

        if len(A) != len(GreaterArray):
            A = extend_array(A, GreaterArray)
        if len(B) != len(GreaterArray):
            B = extend_array(B, GreaterArray)
        if len(C) != len(GreaterArray):
            C = extend_array(C, GreaterArray)
        if len(D) != len(GreaterArray):
            D = extend_array(D, GreaterArray)


        A = adjust_pan(A, self.pan1_value)
        B = adjust_pan(B, self.pan2_value)
        C = adjust_pan(C, self.pan3_value)
        D = adjust_pan(D, self.pan4_value)

        AB = audio_sum(A, B)
        CD = audio_sum(C, D)
        E = audio_sum(AB, CD)

        nframes = len(GreaterArray)
        comptype = "NONE"
        compname = "not compressed"
        bounce = wave.open(self.nameEntry.get() + ".wav", "wb")
        bounce.setparams((self.channels, self.bitdepth/8, int(44100*2), int(nframes), comptype, compname))
        byte = array.array.tostring(E)
        bounce.writeframes(byte)
        bounce.close()
        print("Bounce completed successfully")
#definición metodo main que ejecuta la ventana
def Main():
    app = Window()
    return 0


if __name__ == "__main__":
    Main()