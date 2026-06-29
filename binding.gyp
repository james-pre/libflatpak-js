{
  "targets": [
    {
      "target_name": "flatpak",
      "sources": [
        "src/flatpak.cc"
      ],
      "include_dirs": [
        "<!@(node -p \"require('node-addon-api').include\")"
      ],
      "cflags": [
        "<!@(pkg-config --cflags flatpak)"
      ],
      "cflags_cc": [
        "<!@(pkg-config --cflags flatpak)",
        "-std=c++17",
        "-fexceptions",
        "-Wall",
        "-Wextra"
      ],
      "libraries": [
        "<!@(pkg-config --libs flatpak)"
      ],
      "defines": [
        "NAPI_VERSION=8"
      ],
      "conditions": [
        ["OS!='win'", {
          "cflags+": [
            "-fvisibility=hidden"
          ]
        }]
      ],
      "dependencies": [
        "<!(node -p \"require('node-addon-api').gyp\")"
      ]
    }
  ]
}
