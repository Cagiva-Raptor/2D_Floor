// 2D Floorplan · 08:00 12:00 16:00 22:00 tijdvakken · 55 entities

export const slots = ["08:00", "12:00", "16:00", "22:00"];

// Entities uit dit dashboard, per domein. Wat hier niet staat, staat niet op de plattegrond.
export const entities = {
  binary_sensor: ["binary_sensor.lumi_achterdeur", "binary_sensor.lumi_garagepoort"],
  camera: [
    "camera.192_168_4_53_living_high",
    "camera.g3_pro_buiten_medium_resolution_channel",
    "camera.g4_doorbell_high",
    "camera.garage_fluent",
    "camera.k12_kiosk",
  ],
  climate: ["climate.nbp6013h"],
  cover: ["cover.garage_door", "cover.rfy_090301_1", "cover.rfy_090f0f_1"],
  input_boolean: [
    "input_boolean.2d_dummy_items",
    "input_boolean.2d_ff_garage",
    "input_boolean.bmw_in_tha_house",
    "input_boolean.lamp_hal_achteraan",
    "input_boolean.lamp_hal_vooraan",
    "input_boolean.lamp_keuken_aanrecht",
    "input_boolean.lamp_keuken_eilend",
    "input_boolean.lamp_keuken_tafel",
    "input_boolean.lamp_living_links",
    "input_boolean.lamp_living_rechts",
    "input_boolean.lamp_toilet",
    "input_boolean.lamp_vaulte",
    "input_boolean.lamp_waskot",
    "input_boolean.renault_in_garage",
    "input_boolean.xmas",
  ],
  input_text: ["input_text.floorplan_label_1everdiep", "input_text.floorplan_label_2everdiep"],
  light: [
    "light.kajplats_e27_ws_g95_clear_806lm",
    "light.kerstverlichting",
    "light.sfeerspots_haard",
    "light.sonoff_stekker_02_light",
    "light.sonoff_stekker_03_light",
    "light.trust_international_b_v_zll_dimmableligh_level_on_off",
    "light.trust_international_b_v_zll_extendedcolo_level_light_color_on_off",
  ],
  media_player: ["media_player.homepod_mini", "media_player.lg_tv_living", "media_player.sonos_keuken"],
  person: ["person.amber", "person.jurgen", "person.loena", "person.nancy", "person.yarne"],
  sensor: [
    "sensor.charger_status",
    "sensor.fairland_current_temperature",
    "sensor.fairland_target_temperature",
    "sensor.p1_totaal",
    "sensor.power_consumption_dashboard",
    "sensor.power_production_dashboard",
    "sensor.timeofday",
    "sensor.total_solar_production_dashboard",
  ],
  switch: [
    "switch.fairland_power",
    "switch.niko_koffiezet_on_off",
    "switch.niko_nv_connected_socket_outlet_switch",
    "switch.sonoff_stekker_07",
  ],
};

/**
 * Alles van dit dashboard op één plek.
 */
export const alles = Object.entries(entities).flatMap(([, ids]) => ids);

export default { slots, entities, alles };

// -----------------------------------------------------------------------------
// Zes tijdvarianten, overgenomen uit de kaart `custom:floorplan-editor` die hier
// stond tot 2 oktober 2026. Die kaart is vervangen; dit is de enige plek waar
// dit nog staat.
//
// Dit is NIET de rotatie die je nu ziet. Die loopt over `sensor.timeofday` in
// templates.yaml, dat dezelfde zes namen kent maar op een andere manier bepaalt:
// zonshoogte en kloktijd, geen zon-events met marge. Vergelijk:
//   hier          ochtend = zonsopgang +90 min
//   timeofday     ochtend = sensor.time tussen 06:00 en 09:00
// Wie hiervan de waarheid is, is een keuze die nog niet gemaakt is.
// -----------------------------------------------------------------------------
export const zonvarianten = [
  {
    id: "night",
    map: "night",
    label: "Nacht",
    basis: "local/jdi_custom_images/floorplans/first_floor/night/",
    achtergrond: "home.png",
    grens: { type: "clock", at: "00:00" },
  },
  {
    id: "sunrise",
    map: "sunrise",
    label: "Zonsopgang",
    basis: "local/jdi_custom_images/floorplans/first_floor/sunrise/",
    achtergrond: "home.png",
    grens: { type: "sun", event: "sunrise", offset: 0 },
  },
  {
    id: "morning",
    map: "morning",
    label: "Ochtend",
    basis: "local/jdi_custom_images/floorplans/first_floor/morning/",
    achtergrond: "home.png",
    grens: { type: "sun", event: "sunrise", offset: 90 },
  },
  {
    id: "day",
    map: "day",
    label: "Dag",
    basis: "local/jdi_custom_images/floorplans/first_floor/day/",
    achtergrond: "home.png",
    grens: { type: "sun", event: "sunrise", offset: 270 },
  },
  {
    id: "sunset",
    map: "sunset",
    label: "Zonsondergang",
    basis: "local/jdi_custom_images/floorplans/first_floor/sunset/",
    achtergrond: "home.png",
    grens: { type: "sun", event: "sunset", offset: 0 },
  },
  {
    id: "evening",
    map: "evening",
    label: "Avond",
    basis: "local/jdi_custom_images/floorplans/first_floor/evening/",
    achtergrond: "home.png",
    grens: { type: "sun", event: "sunset", offset: 60 },
  },
];
