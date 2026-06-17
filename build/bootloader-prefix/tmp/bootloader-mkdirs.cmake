# Distributed under the OSI-approved BSD 3-Clause License.  See accompanying
# file Copyright.txt or https://cmake.org/licensing for details.

cmake_minimum_required(VERSION 3.5)

file(MAKE_DIRECTORY
  "D:/ESP/Espressif/frameworks/esp-idf-v5.3.1/components/bootloader/subproject"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/tmp"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/src/bootloader-stamp"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/src"
  "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/src/bootloader-stamp"
)

set(configSubDirs )
foreach(subDir IN LISTS configSubDirs)
    file(MAKE_DIRECTORY "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/src/bootloader-stamp/${subDir}")
endforeach()
if(cfgdir)
  file(MAKE_DIRECTORY "G:/1_Personal_File/chessAssistant/programV2.0/programV2.1/build/bootloader-prefix/src/bootloader-stamp${cfgdir}") # cfgdir has leading slash
endif()
