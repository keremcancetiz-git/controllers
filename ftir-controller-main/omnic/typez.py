from enum import Enum


class Command(Enum):
    Delay = "Delay"
    Export = "Export"
    CollectSample = "Invoke CollectSample Auto Polling"
    DisplayLimits = "DisplayLimits"
    Display = "Display"
    
    def __str__(self):
        return self.value


class Result(Enum):
    Current = "Current"

    def __str__(self):
        return self.value
