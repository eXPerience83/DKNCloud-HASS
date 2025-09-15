# ❄️ DKN Cloud for HASS

**Control your Daikin Airzone Cloud (dkn.airzonecloud.com) HVAC systems natively from Home Assistant.**  
Optimized for the "DAIKIN ES.DKNWSERVER Wifi adapter" — climate, fan, diagnostics, and temperature at your fingertips.

[![GitHub Release][release-shield]][release-url]
[![License][license-shield]](LICENSE)
[![hacs][hacs-shield]][hacs-url]
[![PRs Welcome][prs-shield]][prs-url]
[![Python][python-shield]][python-url]
[![Made with love][love-shield]][love-url]

[release-shield]: https://img.shields.io/github/release/eXPerience83/DKNCloud-HASS.svg?style=flat
[release-url]: https://github.com/eXPerience83/DKNCloud-HASS/releases
[license-shield]: https://img.shields.io/github/license/eXPerience83/DKNCloud-HASS.svg?style=flat
[hacs-shield]: https://img.shields.io/badge/HACS-Custom-orange.svg?style=flat
[hacs-url]: https://hacs.xyz
[prs-shield]: https://img.shields.io/badge/PRs-welcome-brightgreen.svg?style=flat
[prs-url]: https://github.com/eXPerience83/DKNCloud-HASS/pulls
[python-shield]: https://img.shields.io/badge/python-3.11%2B-blue
[python-url]: https://www.python.org/
[love-shield]: https://img.shields.io/badge/made%20with-%E2%9D%A4-red
[love-url]: https://github.com/eXPerience83

---

![Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/raw/master/screenshot.png)

---

## 🚀 Features

- **Fully integrated climate control:**  
  Power, mode (heat/cool/fan/dry), target temperature, and fan speed for each unit.
- **Automatic device/sensor creation:**  
  Creates climate, temperature, diagnostic, and power entities for each device.
- **Diagnostic sensors:**  
  Monitor device states, available modes, slats, scene/presets, and more.
- **Zero YAML required:**  
  All configuration via Home Assistant UI.
- **Compatible with HACS:**  
  Easy install & updates.
- **Smooth UX:**
  Optimistic updates for mode/temperature/fan changes with a short delayed refresh, so the UI feels instant while the backend confirms.

---

## 🧭 Mode Mapping

| P2 Value | Home Assistant Mode | Description                 |
|----------|--------------------|-----------------------------|
| `"1"`    | COOL               | Cooling                     |
| `"2"`    | HEAT               | Heating                     |
| `"3"`    | FAN_ONLY           | Ventilation only            |
| `"5"`    | DRY                | Dehumidify                  |

> **Note:**  
> Dual setpoint/auto (HEAT_COOL) mode is not implemented. Real-world testing resulted in the device switching to an undocumented “mode 6” but never activating true dual mode. See [info.md](./info.md) for technical details and command mapping.

---

## ⚙️ Installation

### HACS (Recommended)
1. Go to **HACS → Integrations**
2. Click **⋮ → Custom repositories**
3. Add: `https://github.com/eXPerience83/DKNCloud-HASS`
4. Search & install **DKN Cloud for HASS**
5. **Restart** Home Assistant

### Manual
1. Copy `airzoneclouddaikin` folder into `custom_components` in your HA config directory
2. **Restart** Home Assistant

---

## 🔧 Configuration

After installation, go to **Settings → Devices & Services → Add Integration** and search for **DKN Cloud for HASS**.  
Enter your Airzone Cloud **username** and **password**.

**Optional parameters:**
- **Scan interval:** Data refresh interval (seconds, default: 10)

> **No YAML required!**  
> All options are set via the Home Assistant UI.

---

## 🏷️ What You Get

- **Climate entity:**  
  - All core modes (COOL, HEAT, FAN, DRY)
  - Dynamic fan speed control
- **Sensor entities:**  
  - Current temperature (`local_temp`)
  - Diagnostics: modes, scenes, program status, slats, etc.
- **Switch entity:**  
  - Power ON/OFF per device

> Full API/command mapping and advanced usage in [info.md](./info.md).

---

## 📷 Screenshots

![Panel Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/raw/master/screenshot.png)

---

## 🧪 Compatibility

| Home Assistant | Python | Daikin Model/Adapter         |
|----------------|--------|-----------------------------|
| 2025.4+        | 3.11+   | DAIKIN ES.DKNWSERVER (Cloud)|

*Other Airzone or Daikin adapters may not be supported.*

---

## 🛣️ Roadmap

- [ ] Multi-language support for sensors and diagnostics
- [ ] More diagnostics and error reporting

---

## ❓ FAQ / Troubleshooting

**Q: Why can't I set dual temperatures or use "auto" mode?**  
A: Although the API and some docs suggest support for a dual setpoint (`HEAT_COOL`) or "auto" mode (P2=4), all real-world testing on the DKN/Daikin hardware resulted in the device switching to "mode 6" (undocumented) and never actually activating dual mode as intended.  
Because the feature could not be made stable or reliable, it is not implemented in this integration. Further investigation may be needed for future versions.

**Q: Can I control vertical/horizontal slats?**  
A: Slat state/position is shown in diagnostic sensors; control is not implemented but fields are exposed for advanced users.

**Q: What about scene/presets?**  
A: Current scene (occupied, sleep, etc.) is available as a diagnostic sensor. Changing preset from HA is not yet implemented.

**Q: Where can I find advanced API usage, all device fields, and curl examples?**  
A: See [info.md](./info.md).

---

## 🔒 Security Notice

**Never share your API token, credentials, or installation IDs in public!**  
Always use placeholders as in this documentation.  
For details on privacy and raw API responses, see [info.md](./info.md).

---

## 🤝 How to Contribute

- Pull requests for features, fixes, or translations are welcome!
- Report bugs or suggest features in [GitHub Issues](https://github.com/eXPerience83/DKNCloud-HASS/issues)

---

## ❤️ Contributing & Support

If you find this integration useful, you can support development via:

- [Ko-fi](https://ko-fi.com/experience83)
- [PayPal](https://paypal.me/eXPerience83)

---

### Networking & Reliability

This integration uses a per-request timeout of **15s** and **exponential backoff with jitter** for `429/5xx` responses.  
If the backend is temporarily unavailable, Home Assistant will retry the config entry startup (**ConfigEntryNotReady**).  
For privacy, logs **never** print your email or token.

---

## 🙏 Acknowledgments

This project was inspired by and originally based on:

- [AirzoneCloudDaikin (PyPI)](https://pypi.org/project/AirzoneCloudDaikin/) and its Home Assistant integration by [max13fr](https://github.com/max13fr/AirzoneCloudDaikin)

Many thanks to those projects and authors for their groundwork and inspiration!

---

## 📜 License

MIT © [eXPerience83](LICENSE)

> This project is not affiliated with or endorsed by Daikin or Airzone. All trademarks are property of their respective owners.
