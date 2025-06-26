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
[python-shield]: https://img.shields.io/badge/python-3.9%2B-blue
[python-url]: https://www.python.org/
[love-shield]: https://img.shields.io/badge/made%20with-%E2%9D%A4-red
[love-url]: https://github.com/eXPerience83

---

![Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/raw/master/screenshot.png)

---

## 🚀 Features

- **Fully integrated climate control:**  
  Power, mode (heat/cool/fan/dry/auto), target temperature(s), and fan speed for each unit.
- **Dual setpoint support (HEAT_COOL):**  
  Adjust both heating and cooling consigns in compatible models.
- **Automatic device/sensor creation:**  
  Creates climate, temperature, diagnostic, and power entities for each device.
- **Diagnostic sensors:**  
  Monitor device states, available modes, slats, scene/presets, and more.
- **Zero YAML required:**  
  All configuration via Home Assistant UI.
- **Compatible with HACS:**  
  Easy install & updates.

---

## 🧭 Mode Mapping

| P2 Value | Home Assistant Mode | Description                 |
|----------|--------------------|-----------------------------|
| `"1"`    | COOL               | Cooling                     |
| `"2"`    | HEAT               | Heating                     |
| `"3"`    | FAN_ONLY           | Ventilation only            |
| `"4"`    | HEAT_COOL          | Heat/Cool (dual setpoint)   |
| `"5"`    | DRY                | Dehumidify                  |

**Full API field/command mapping and advanced usage in [info.md](./info.md).**

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
- **Force Heat/Cool mode:** Always expose HEAT_COOL mode, even if not reported by your machine (use at your own risk)

> **No YAML required!**  
> All options are set via the Home Assistant UI.

---

## 🏷️ What You Get

- **Climate entity:**  
  - All core modes (COOL, HEAT, FAN, DRY, HEAT_COOL)  
  - Both heating & cooling setpoints in HEAT_COOL mode
  - Dynamic fan speed control
- **Sensor entities:**  
  - Current temperature (`local_temp`)
  - Diagnostics: modes, scenes, program status, slats, etc.
- **Switch entity:**  
  - Power ON/OFF per device

\* Setpoints and fan speeds are sent for both cold and heat in dual mode (see [info.md](./info.md) for API details).

---

## 📷 Screenshots

![Panel Screenshot](https://github.com/eXPerience83/DKNCloud-HASS/raw/master/screenshot.png)

---

## 🧪 Compatibility

| Home Assistant | Python | Daikin Model/Adapter         |
|----------------|--------|-----------------------------|
| 2025.4+        | 3.9+   | DAIKIN ES.DKNWSERVER (Cloud)|

*Other Airzone or Daikin adapters may not be supported.*

---

## 🛣️ Roadmap

- [ ] Multi-language support for sensors and diagnostics
- [ ] More diagnostics and error reporting

---

## ❓ FAQ / Troubleshooting

**Q: Why do I see both high and low setpoints in some modes?**  
A: Dual setpoint (`HEAT_COOL`) mode lets you set minimum (cool) and maximum (heat) temperatures simultaneously if your hardware supports it.

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

## 🙏 Acknowledgments

This project was inspired by and originally based on:

- [AirzoneCloudDaikin (PyPI)](https://pypi.org/project/AirzoneCloudDaikin/) and its Home Assistant integration by [max13fr](https://github.com/max13fr/AirzoneCloudDaikin)

Many thanks to those projects and authors for their groundwork and inspiration!

---

## 📜 License

MIT © [eXPerience83](LICENSE)

> This project is not affiliated with or endorsed by Daikin or Airzone. All trademarks are property of their respective owners.
