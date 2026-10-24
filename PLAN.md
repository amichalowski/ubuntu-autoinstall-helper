#skrypt do obsługi autoinstall.yaml z ubuntu

Wymagania
1. skrypt w python3 tylko biblioteki standarowe bez pip
2. skrypt trzema się zasad clean code


## Feature 1 - walidacja pliku autoinstall.yaml

1. skrypt przyjmuje argument  --validate / -v
autoinstall-helper.py --validate autoinstall.yam;
jeśli plik nie istnieje to bład

2. skrypt odczytuj plik autoinstal.yaml
pole autoinstall.refresh-installer.channel
```yaml
autoinstall:
  version: 1
  
  refresh-installer:
    update: true
    channel: "26.04/stable"
```

2. następnie wywołuje narzędzie snap żeby sprawdzić dokładną wersję instalatora

```bash
snap info ubuntu-desktop-bootstrap | grep 26.04/stable
#przykładowy output: 
#  26.04/stable:     26.04.1         2026-09-04 (665) 117MB classic
```
wersja znajduje się w 2 kolumnie

3. należy przygotować Dockerfile
3.1. jakiś mały obsraz bazowy z python3 i git
3.2 Dokcerfile jako ARG przyjmuje wersje instalatora ustaloną w poprzednim punkcie
3.3. robi git clone (shallow copy) z tagiem który jest numerem wersji z ARG https://github.com/canonical/subiquity.git
3.4. jako CMD wystawia plik do walidacji scripts/validate-autoinstall-user-data.py
3.5 obraz jest otagowny po zbudowaniu jako ubuntu-desktop-bootstrap:<wersja_snapa>

4. skrypt python sprawdza czy istnieje juś obraz dla danej wersji jeśli nie to go buduje

5. skrypt uruchmia walidację pliku przez obraz

## Feature 2 --help
skrypt powinin być w stanie wyświetlić help na --help -h

