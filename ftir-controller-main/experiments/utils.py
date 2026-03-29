"""
Returns the absorbance at the closest wavenumber in a spectrum file.
"""

import numpy as np
import pandas as pd

def pick_absorbance(address: str, wavenumber: float) -> float:
    spectrum = pd.read_csv(
        address,
        names=['Wavenumber', 'Absorbance'],
        dtype={'Wavenumber': 'float64', 'Absorbance': 'float64'},
        na_values='#NaN'
    )
    idx = np.argmin(np.abs(spectrum['Wavenumber'] - wavenumber))
    return float(spectrum.iat[idx, 1])
