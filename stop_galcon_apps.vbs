Function StopGalconApps()
    On Error Resume Next
    Set shell = CreateObject("WScript.Shell")
    shell.Run "taskkill.exe /f /im GalconControlGUI.exe", 0, True
    shell.Run "taskkill.exe /f /im GalconMqttTray.exe", 0, True
    shell.Run "taskkill.exe /f /im galcon-mqtt.exe", 0, True
    StopGalconApps = 1
End Function