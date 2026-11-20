from tkinter import *
from tkinter import ttk, font, Canvas, PhotoImage, messagebox
from tkinter.filedialog import askopenfilename
import array, wave, threading, pyaudio

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
            C.append(A[i] + B[i])
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
        def open4(self):
            self.archive4 = askopenfilename(filetypes=(("wav files", "*.wav"), ("All files",
                                                                                "*.*")))  # abre el cuadro de dialogo que permite seleccionar la ruta del archivo permitiendo solo archivos .wav
            Tk().destroy()  # destruye el cuadro de dialogo abierto para seleccionar la ruta
            self.audio4 = wave.open(self.archive4,
                                    'rb')  # objeto archive del tipo wave.open que abre un archivo wav en concreto en modo lectura
            channels = self.audio4.getnchannels()  # obtiene el numero de canales del archivo
            samplefreq = self.audio4.getframerate()  # obtiene la frecuencia de muestreo
            bitdepth = self.audio4.getsampwidth() * 8  # obtiene la profundidad en bits, tomandolo en bytes y convirtiendo a bits
            length = int(
                self.audio4.getnframes() / samplefreq)  # obtiene la duracion del archivo de audio en segundos a partir del numero de cuadros y la frecuencia de muestreo
            for i in range(60):  # ciclo que convierte la duracion en segundos a minutos y segundos
                min = int(length / 60)  # se toman como entero los minutos que dura el archivo
                sec = length % 60  # el resto (modulo) de la division serán los segundos
            # se muestre por consola la ruta del archivo, el numero de canales, la frecuencia de muestreo, la profundidad en bits y la duración del archivo
            print('Channel 4 audio loaded '
                  '\n File route: ', self.name,
                  '\n Number of channels: ', channels,
                  '\n Sample rate: ', samplefreq,
                  '\n Bit Depth: ', bitdepth,
                  '\n Duration:', min, ':', sec)
            self.audio4.close()

        # definicion del metodo play para reproducir el archivo seleccionado
        def play(self):  # terminar metodo play para poder reproducir la suma de los 4 archivos
            print("Now playing...")
            audio = pyaudio.PyAudio()  # se crea un objeto audio del tipo pyaudio
            audio1 = wave.open(self.archive1, 'rb')
            audio2 = wave.open(self.archive2, 'rb')
            audio3 = wave.open(self.archive3, 'rb')
            audio4 = wave.open(self.archive4, 'rb')
            # se crea un objeto stream que permitira que flujo de informacion del archivo
            stream = audio.open(format=audio.get_format_from_width(audio1.getsampwidth()),
                                # obtiene la profundidad de bits
                                channels=audio1.getnchannels(),  # se obtiene el numero de canales
                                rate=audio1.getframerate(),  # se obtiene la frecuencia de muestreo
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
                data = array.array.tostring(
                    E)  # el arreglo modificado en su volumen se convierte a string para ser leido y reproducido
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
        # definicion del metodo press_play que determina el estado del programa(reproduciendo o no)
        def press_play(self):
            if not self.archive1 and not self.archive2 and not self.archive3 and not self.archive4:  # si no se ha seleccionado un archivo se manda un mensaje
                messagebox._show(message="There's no audio file opened to play. Please choose .wav audio files")
            else:  # al estar seleccionado un archivo se procede a reproducir
                if not self.playing:  # si no está reproduciendo procede a reproducir, y si esta reproduciendo continua con la reproduccion actual
                    self.playing = True  # el estado de programa se vuelve reproduciendo
                    self.thread = threading.Thread(
                        target=self.play)  # se crea sobre thread un objeto thread( un hilo) de ejecución donde se ejecuta el metodo play
                    self.thread.start()  # inicia el hilo de ejecución creado
        # definción del metodo stop que detiene la reproduccion del archivo
        def stop(self):
            if self.playing:  # solo puede detener la reproducción si el estado del programa es reproduciendo
                self.playing = False  # el estado de programa ya no es reproduciendo
                print("Stopped")
                self.thread.join()  # espera a que el hilo en ejecucion se termine
                # definicion de metodos set volume que toma el valor del fader, retorna un float

        def set_vol1(self, v1_slider):
            self.volume1_value = v1_slider

        def set_vol2(self, v2_slider):
            self.volume2_value = v2_slider

        def set_vol3(self, v3_slider):
            self.volume3_value = v3_slider

        def set_vol4(self, v4_slider):
            self.volume4_value = v4_slider
        # definicion del metodos set pan que toma el valor del fader, retorna un float

        def set_pan1(self, p1_slider):
            self.pan1_value = p1_slider

        def set_pan2(self, p2_slider):
            self.pan1_value = p2_slider

        def set_pan3(self, p3_slider):
            self.pan1_value = p3_slider

        def set_pan4(self, p4_slider):
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
            bounce.setparams((self.channels, self.bitdepth / 8, int(44100), int(nframes / 2), comptype, compname))
            byte = array.array.tostring(E)
            bounce.writeframes(byte)
            bounce.close()
            print("Bounce completed successfully")

# definición metodo main que ejecuta la ventana
def Main():
    app = Window()
    return 0

if __name__ == "__main__":
    Main()