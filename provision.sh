sudo wget -O /etc/udev/rules.d/99-backlight.rules https://raw.githubusercontent.com/medunigraz/outpost.attendance.kivy/master/99-backlight.rules
wget -O attendance.py https://raw.githubusercontent.com/medunigraz/outpost.attendance.kivy/master/attendance.py
sudo apt -y update
sudo apt -y upgrade
sed -i "s,log_enable = 1,log_enable = 0," .attendance.ini .kivy/config.ini
echo -e "rm -f ~/.kivy/logs/*\n$(cat .xinitrc)" > .xinitrc
sudo reboot
