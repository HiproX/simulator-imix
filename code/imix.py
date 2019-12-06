#!/usr/bin/env python3

import threading
import socket
import argparse
import configparser
import os
import sys
import pickle
import re
import csv
from time import time, sleep
import random
import copy
import queue
from datetime import datetime
from operator import itemgetter

# Информация об Internet MIX.
# Процентное соотношение размеров пакетов в корпоративной сети.
IMIX_info = (
    {'distribution': 0.07, 'package_size': 40},
    {'distribution': 0.56, 'package_size': 576},
    {'distribution': 0.37, 'package_size': 1448}
)

TYPES_DATA = ('b', 'Kb', 'Mb', 'Gb')
BYTES, KBYTES, MBYTES, GBYTES = range(4)  # размеры данных
FLG_UDP, FLG_TCP = range(2)  # тип протокола
BUFFER_SIZE = 1448  # размер буфера ожидаемых данных в байтах
CSV_NAME = 'imix_log.csv'
MIN_PORT, MAX_PORT = 0, 65533

# Параметры по умолчанию для аргументов командной строки
DEFAULT_PORT = 1060
DEFAULT_CYCLES = 3
DEFAULT_RATIO = 0.5
DEFAULT_ORDERBY = 'RAND'
DEFAULT_TIMEOUT_UDP = 2.0
DEFAULT_VOLUME = '10Mb'

# Для файла конфигураций
CONFIG_NAME = 'imix_config.ini'
CLIENT_SECTION_NAME = 'Client'
ERR_CONFIG_NO_SECTION = 300
ERR_CONFIG_NO_OPTION = 301


def create_argument_parser():
    """
    Создания и инициализация парсера для аргументов командной строки/терминала.
    Возвращает парсер.
    """
    def is_valid_port(parser, port):
        """
        Валидный ли порт.
        """
        try:
            port = int(port)
            if MIN_PORT <= port <= MAX_PORT:
                    return port
            raise ValueError
        except ValueError:
            parser.error(f'аргумент --port/-p: невалидный (выбирайте из диапазона [{MIN_PORT}, {MAX_PORT}])')

    def is_valid_volume(parser, volume):
        """
        Валидный ли объем данных, который будет получен от сервера.
        """
        match = re.search(r'^\d+\w{1,2}$', volume)
        if not match:
            parser.error(f'Не правильно задан объем данных ({volume}).')
        volume = volume.lower()
        number, type_data = None, None
        bound = -1  # граница между числом и типом
        # Если тип занимает 2 символа, то смещаем границу
        if volume[-2:].isalpha():
            bound = -2
        # Отделение числа от типа
        number, type_data = int(volume[:bound]), volume[bound:]
        # Кортеж доступных типов и их константы
        types = (('b', BYTES), ('kb', KBYTES), ('mb', MBYTES), ('gb', GBYTES))
        for item, constant in types:
            # Если есть такой тип, то возвращает число отделенное от типа
            if item == type_data:
                return (number, constant)
        parser.error('аргумент --volume/-v: невалидный (используйте N{b|Kb|Mb|Gb}, где N целое положительное число)')

    def is_valid_ratio(parser, ratio):
        """
        Валидное ли соотношение TCP & UDP пакетов,
        которые будут получены от сервера.
        """
        try:
            ratio = float(ratio)
            if ratio < 0 or ratio > 1:
                raise ValueError
            return ratio
        except ValueError:
            parser.error('аргумент --ratio/-r: невалидный (выбирайте из диапазона[0, 1.0]).')

    # Создание аргументов
    parser = argparse.ArgumentParser()
    parser.add_argument('--server', '-s',
                        action='store_true',
                        help='Запустить сервер.')
    parser.add_argument('--client', '-c',
                        action='store_true',
                        help='Запусть клиент. Как аргумент принимает IP-адрес сервера к которому нужно подключиться.')
    parser.add_argument('--address', '-a',
                        nargs='?',
                        type=str,
                        metavar='IP_ADDRESS',
                        help='IP-адрес сервера для подключения клиента.')
    parser.add_argument('--port', '-p',
                        nargs='?',
                        type=lambda port: is_valid_port(parser, port),
                        default=DEFAULT_PORT,
                        help=f'Порт N => (N, N + 1, N + 2). По умолчанию порт {DEFAULT_PORT} {tuple(DEFAULT_PORT + x for x in range(3))}.')
    parser.add_argument('--volume', '-v',
                        nargs='?',
                        default=DEFAULT_VOLUME,
                        type=lambda volume: is_valid_volume(parser, volume),
                        help=f'Объем получаемых данных от сервера: N{{b|Kb|Mb|Gb}}, где N целое положительное число. По умолчанию {DEFAULT_VOLUME}.')
    parser.add_argument('--ratio', '-r',
                        nargs='?',
                        type=lambda ratio: is_valid_ratio(parser, ratio),
                        default=DEFAULT_RATIO,
                        help=f'Процентное соотношение TCP & UDP пакетов. По умолчанию {DEFAULT_RATIO}.')
    parser.add_argument('--orderby', '-o',
                        choices=['RAND', 'ASC', 'DESC'],
                        default=DEFAULT_ORDERBY,
                        type=str,
                        help='Определяет порядок пакетов по их размеру. RAND - случайный. ASC - по возрастанию. DESC - по убыванию.')
    parser.add_argument('--cycles', '-clc',
                        nargs='?',
                        type=int,
                        default=DEFAULT_CYCLES,
                        help=f'Количество замеров. По умолчанию {DEFAULT_CYCLES}.')
    parser.add_argument('--config', '-cfg',
                        action='store_true',
                        help=f"Использовать конфиг. Конфиг '{CONFIG_NAME}' должен находится в одном окружении с проектом. При отсутствии конфига установите данный флаг, для создания конфига с параметрами по умолчанию.")
    parser.add_argument('--csv',
                        action='store_true',
                        help=f"Сохранять результаты в CSV файл '{CSV_NAME}'. Файл создается в одном окружении с проектом.")
    return parser


def parse(args):
    """
    Парсить.
    Отвечает за парсинг и обработку аргументов командной строки.
    """
    parser = create_argument_parser()
    # Вывод help
    if not len(args) or '-h' in args or '--help' in args:
        parser.print_help()
        sys.exit(0)
    # Иначе парсинг аргументов
    return parser, parser.parse_args(args[1:])


def parse_config(path):
    """
    Разбор файла конфигураций.
    Возвращает строку аргументов.
    """
    # Аргументы командной строки (для конфига)
    fields = {
            'address': '127.0.0.1',
            'port': DEFAULT_PORT,
            'ratio': DEFAULT_RATIO,
            'volume': DEFAULT_VOLUME,
            'orderby': DEFAULT_ORDERBY,
            'cycles': DEFAULT_CYCLES
    }

    def create_config(path):
        """
        Создать файл конфигураций.
        """
        config = configparser.ConfigParser()
        config.add_section(CLIENT_SECTION_NAME)
        for key, value in fields.items():
            config.set(CLIENT_SECTION_NAME, key, value if value is str else str(value))
        with open(CONFIG_NAME, 'w') as file:
            config.write(file)

    def get_config(path):
        """
        Возвращает объект конфигураций.
        """
        # Если конфигурационный файл отсутствует - создаем
        if not os.path.exists(path):
            create_config(path)
            print('Конфигурационный файл отсутствует.')
            print(f'В окружении с проектом создан конфигурационный файл {CONFIG_NAME}.')
            print(f'Теперь Вы можете использовать конфигурационный файл для запуска клиента.')
            sys.exit(1)

        # Разбир конфигурационного файла
        config = configparser.ConfigParser()
        config.read(path)
        return config
    # Получение конфига
    config = get_config(path)
    args = ['--client', ]
    # Считывание аргументов
    try:
        for key in fields.keys():
            value = None
            try:
                value = config.get(CLIENT_SECTION_NAME, key)
            except configparser.NoOptionError as e:
                if key == 'address':
                    print(e.args[0])
                    sys.exit(ERR_CONFIG_NO_OPTION)
            if value:
                args.extend([f'--{key}', value])
    except configparser.NoSectionError as e:
        print(e.args[0])
        sys.exit(ERR_CONFIG_NO_SECTION)
    return args


def check_args(parser, namespace):
    """
    Проверка аргументов на порядок следования.
    """
    if namespace.server and namespace.client:
        parser.error('аргументы --server/-s и --client/-c: сервер и клиент не могут быть одновременно запущенны из одного процесса.')
    elif namespace.config and namespace.server:
        parser.error('Серверу не требуется конфигурационный файл.')
    elif namespace.config and namespace.client:
        args = parse_config(CONFIG_NAME)
        namespace = parser.parse_args(args)
    return namespace


class ExThread(threading.Thread):
    """
    Обертка над потоком.
    Позволяет выводить исключения в главный поток.
    """
    def __init__(self, daemon=None):
        threading.Thread.__init__(self, daemon=daemon)
        self.__status_queue = queue.Queue()

    def run_with_exception(self):
        """Этот метод должен быть перегружен"""
        raise NotImplementedError

    def run(self):
        """Этот метод не должен быть перегружен"""
        try:
            self.run_with_exception()
        except BaseException:
            self.__status_queue.put(sys.exc_info())
        self.__status_queue.put(None)

    def wait_for_exc_info(self):
        return self.__status_queue.get()

    def join_with_exception(self):
        ex_info = self.wait_for_exc_info()
        if ex_info is None:
            return
        else:
            raise ex_info[1]


class ThreadEx(ExThread):
    """
    Поток с возможностью вывода исключений в главный поток.
    """
    def __init__(self, target=None, args=None, daemon=None):
        ExThread.__init__(self, daemon)
        self.target = target
        self.args = args

    def run_with_exception(self):
        if self.args is None:
            self.target()
        else:
            self.target(*self.args)


class Server:
    def __init__(self, address, UDP_port, TCP_port, GetOptions_port):
        # Is valid ports
        self.__is_valid_port(UDP_port)
        self.__is_valid_port(TCP_port)
        # Address
        self.__address = copy.deepcopy(address)
        # Ports
        self.__UDP_port = copy.deepcopy(UDP_port)
        self.__TCP_port = copy.deepcopy(TCP_port)
        self.__GetOptions_port = copy.deepcopy(GetOptions_port)
        # Sockets
        self.__sUDP = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.__sTCP = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.__sGetOptions = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Events
        self.__event_OnGetOptions = threading.Event()
        self.__event_OnSendPackage = threading.Event()
        # Data
        self.__reset_data()

    def __reset_data(self):
        """
        Сброс данных.
        """
        self._args = None
        self.__client = None
        self.__client_address = None

    def __is_valid_port(self, port):
        """
        Проверка порта на валидность.
        """
        if not(0 <= port < 65535):
            raise socket.error(f'Порт({port}) не может быть меньше 0 и большe 65535')

    def __start_TCP(self):
        if not self.__TCP_port:
            raise Exception('Не указан TCP порт')
        self.__sTCP.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.__sTCP.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.__sTCP.bind((self.__address, self.__TCP_port))
        self.__sTCP.listen(1)

    def start(self):
        """
        Запустить сервер.
        """
        # Запустить TCP
        self.__start_TCP()
        print('[S] Сокет для передачи данных клиенту открыт!')
        # Инициализация потоков
        threads = (
            # Получение параметров с клиента и подключение клиента
            ThreadEx(target=self.__get_options, daemon=True),
            # Отправка пакетов на клиент
            ThreadEx(target=self.__send_package, daemon=True)
        )
        # Запуск потоков
        for thread in threads:
            thread.start()
        # Первым выполняется поток с методом self.__get_options
        self.__event_OnGetOptions.set()
        # Присоединяем потоки
        for thread in threads:
            thread.join_with_exception()
            thread.join()

    def __make_rand_bytes(self, packets_size, quantity_for_each_size=10):
        """
        Создать рандобные байты.
        Принимает размеры данных (пакетов) для которых нужно нарандомить
        и количество таких рандомов.
        Возвращает словарь индексируемый размером данных (пакетов) (int)
        хранящий в себе список объектов bytes со случайными байтами -> {X:[Y, ...], ...} .
        """
        data = {}
        for size in packets_size:
            data[size] = []
            for _ in range(quantity_for_each_size):
                data[size].append(bytes([random.randint(0, 255) for _ in range(size)]))
        return data

    def __wait_OnGetOptions(self):
        self.__event_OnGetOptions.wait()
        self.__event_OnGetOptions.clear()

    def __accept(self, event, sock, result):
        result['client'], result['address'] = sock.accept()
        event.set()

    def __get_options(self):
        """
        Подключение клиента и получение аргументов от клиента.
        """
        self.__sGetOptions.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.__sGetOptions.bind((self.__address, self.__GetOptions_port))
        self.__sGetOptions.listen(1)
        print('[S] Сокет для получения аргументов от клиента открыт!')
        self.__client, self.__client_address = None, None
        while True:
            # Ждем пока сработает событие, чтобы продолжить работу
            self.__wait_OnGetOptions()
            print(f'\n\n[S] **** {datetime.now()} ****')
            print(f'[S] Ожидается подключение клиента...')
            clientInput, clientInput_address = None, None
            is_client_connected = False
            # Пока клиент не подключился...
            while not is_client_connected:
                OnClientConnection = (threading.Event(), threading.Event())
                # Для хранения результатов потоков (threads_accept) описанных ниже
                result_sGetOptions, result_sTCP = {}, {}
                # Подключение клиента по двум сокетам.
                # TCP socket #1 (sGetOptions) для получения аргументов от клиента.
                # TCP socket #2 (sTCP) для последующей передачи данных клиенту.
                threads_accept = (
                    ThreadEx(target=self.__accept,
                             args=(OnClientConnection[0], self.__sGetOptions,
                                   result_sGetOptions),
                             daemon=True),
                    ThreadEx(target=self.__accept,
                             args=(OnClientConnection[1], self.__sTCP,
                                   result_sTCP),
                             daemon=True)
                )
                # Старт потоков
                for thread in threads_accept:
                    thread.start()
                # Ожидание выполнения потоков
                for thread in threads_accept:
                    thread.join_with_exception()
                    thread.join()
                # Дожидается подключения клиента на 2 сокета
                for event in OnClientConnection:
                    event.wait()
                    event.clear()
                # Получаем клиентов и их адреса с 2-ух сокетов
                clientGetOptions, clientGetOptions_address = result_sGetOptions['client'], result_sGetOptions['address']
                self.__client, self.__client_address = result_sTCP['client'], result_sTCP['address']
                # Сверяем адрес клиента на 2-ух сокетах. Если это один и тот же - продолжаем работу
                if clientGetOptions_address[0] == self.__client_address[0]:
                    is_client_connected = True
            print(f'[S] {self.__client_address[0]} подключился')
            try:
                print(f'[S] Ожидание аргументов от {self.__client_address[0]}')

                args = clientGetOptions.recv(4096)
                if args:
                    self.__args = pickle.loads(args)
                    print(f'[S] Аргументы от {self.__client_address[0]} получены')
                    # Переходим к методу self.__run
                    self.__event_OnSendPackage.set()
            except ConnectionResetError:
                print(f'[S] {clientGetOptions_address[0]} разорвал соединение', file=sys.stderr)
            finally:
                clientGetOptions.close()
                print(f'[S] Соединение с {self.__client_address[0]} закрыто')

    def __wait_OnSendPackage(self):
        self.__event_OnSendPackage.wait()
        self.__event_OnSendPackage.clear()

    def __send_package(self):
        while True:
            # Ждем пока сработает событие, чтобы продолжить работу
            self.__wait_OnSendPackage()
            # Парсим пришедшие аргументы
            what_packages_exist, packages = self.__parse_args(self.__args)
            isConnectionResetError = False  # оборвалосьли TCP ли подключение с клиентом
            print(f'[S] Передача данных клиенту {self.__client_address[0]} началась')
            # Создаем несколько наборов из N случайных bytes для каждого размера
            pseudo_rand_bytes = self.__make_rand_bytes(what_packages_exist)
            p = 0
            # Шлем пакеты клиенту
            for item in packages:
                try:
                    stream = random.choice(pseudo_rand_bytes[item['package_size']])
                    if isConnectionResetError:
                        break
                    if item['protocol'] == FLG_UDP:
                        self.__sUDP.sendto(stream, (self.__client_address[0], self.__UDP_port))
                    elif item['protocol'] == FLG_TCP:
                        self.__client.send(stream)
                    else:  # если не известный флаг
                        break
                except ConnectionResetError:
                    isConnectionResetError = True
            self.__client.close()
            print(f'[S] Передача данных клиенту {self.__client_address[0]} завершена')
            # Очищаем данные
            self.__reset_data()
            # Передача данных завершена. Ждем следующего клиента (метод self.__start_Input)
            self.__event_OnGetOptions.set()

    def __parse_args(self, args):
        """
        Парсинг аргументов переданных от клиента: объем, соотношение пакетов, параметр рандомизации.
        Возвращает список с размерами пакетов которые существуют и сами пакеты для TCP & UDP
        в зависимости от соотношения пакетов их переданного объема.
        """
        # Вычисление объема данных в байтах, который должен будет получить клиент
        szBytes = args['volume'][0] * (1024 ** args['volume'][1])
        # Подсчет количества TCP & UDP пакетов
        szTCP = int(szBytes * args['ratio'])
        szUDP = szBytes - szTCP
        what_packages_exist = []  # пакеты какого объема существуют
        packages = []  # пакеты (тип_пакета, размер_данных)
        # TCP - 32byte
        for item in IMIX_info:
            # Количество UDP & TCP пакетов для размера IMIX_info[i]['package_size']
            numbers_UDP = int(szUDP * item['distribution'] / item['package_size'])
            numbers_TCP = int(szTCP * item['distribution'] / item['package_size'])
            # Есть ли UDP & TCP пакеты, если да, то добавляем объем...
            # (понадобится для ф-и self.__make_rand_bytes, чтобы знать какие пакеты нарандомить)
            if numbers_UDP or numbers_TCP:
                what_packages_exist.append(item['package_size'])
            # Генерируем данные
            packs = [{'protocol': FLG_UDP, 'package_size': item['package_size']}, ] * numbers_UDP
            packs += [{'protocol': FLG_TCP, 'package_size': item['package_size']}, ] * numbers_TCP
            random.shuffle(packs)
            packages.extend(packs)
        # По умолчанию список отсортирован.
        # Изменение порядка, если требуется.
        if args['orderby'] == 'RAND':
            random.shuffle(packages)
        elif args['orderby'] == 'DESC':
            packages.reverse()

        return what_packages_exist, packages


class ClientUDP:
    """
    Client UDP a.k.a Server UDP.
    """
    def __init__(self, address, buffer):
        self.__buffer = buffer
        self.__address = copy.deepcopy(address)
        self.__s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def __call__(self):
        self.__s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.__s.bind(self.__address)
        try:
            is_first_package = True
            while True:
                data, _ = self.__s.recvfrom(BUFFER_SIZE)
                if not data:
                    break
                else:
                    # Если это первый пакет - устанавливается время ожидания пакета
                    if is_first_package:
                        is_first_package = False
                        self.__s.settimeout(2)
                    self.__buffer.append((FLG_UDP, len(data), time()))
        except socket.timeout:
            # Если сработал timeout - сбрасываем до дефолта
            self.__s.settimeout(socket.getdefaulttimeout())
        finally:
            self.__s.close()


class ClientTCP:
    """
    Client TCP.
    """
    def __init__(self, address, buffer):
        self.__address = copy.deepcopy(address)
        self.__buffer = buffer
        self.__is_close = False
        self.__s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.__s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    def __call__(self, event_allow):
        # Ждет пока произойдет передача аргументов на сервер
        # (ф-я send_args_to_server)
        event_allow.wait()
        event_allow.clear()
        # Подключение к серверу
        self.__s.connect(self.__address)
        # Пока приходят данные от сервера
        while True:
            # Считываем
            data = self.__s.recv(BUFFER_SIZE)
            if not data:
                break
            else:
                # Сохраняем информацию о протоколе, кол-ве данных в байтах и время получения
                self.__buffer.append((FLG_TCP, len(data), time()))
        self.__s.close()
        self.__is_close = True

    def is_close(self):
        """
        Закрыт ли сокет.
        """
        return self.__is_close


def get_idx_type_data(value):
    N = 0
    for _ in range(len(TYPES_DATA)):
        if value / 1024 > 1:
            value /= 1024
            N += 1
        else:
            break
    return N


def calculate_test(test):
    res = {
        'speed': 0,
        'speed_headers': 0,
        'received_volume': 0,
        'received_volume_headers': 0,
        'received_volume_tcp': 0,
        'received_volume_tcp_headers': 0,
        'received_volume_udp': 0,
        'received_volume_udp_headers': 0,
        'full_time': 0
    }
    PROTOCOL, LEN_BUFF, TIME = range(3)
    test = sorted(test, key=itemgetter(TIME))
    res['full_time'] = test[-1][TIME] - test[0][TIME]
    LEN_UDP_IP_HEADER = 32
    LEN_TCP_IP_HEADER = 56
    # Обрабатывание теста
    for i in test:
        res['received_volume'] += i[LEN_BUFF]
        if i[PROTOCOL] == FLG_UDP:
            res['received_volume_headers'] += i[LEN_BUFF] + LEN_UDP_IP_HEADER
            res['received_volume_udp_headers'] += i[LEN_BUFF] + LEN_UDP_IP_HEADER
            res['received_volume_udp'] += i[LEN_BUFF]
        else:
            res['received_volume_headers'] += i[LEN_BUFF] + LEN_TCP_IP_HEADER
    res['received_volume_tcp'] = res['received_volume'] - res['received_volume_udp']
    res['received_volume_tcp_headers'] = res['received_volume_headers'] - res['received_volume_udp_headers']
    res['speed'] = (res['received_volume'] - test[0][LEN_BUFF]) / res['full_time'] * 8
    if test[0][PROTOCOL] == FLG_UDP:
        fp = test[0][LEN_BUFF] + LEN_UDP_IP_HEADER
    else:
        fp = test[0][LEN_BUFF] + LEN_TCP_IP_HEADER
    res['speed_headers'] = (res['received_volume_headers'] - fp) / res['full_time'] * 8
    return res


def print_test(test):
    res = copy.deepcopy(test)
    idx_type = get_idx_type_data(res['received_volume'])
    res['type'] = TYPES_DATA[idx_type]
    N = 1024 ** idx_type

    res = copy.deepcopy(res)

    for key in res:
        if not(key in ('full_time', 'type')):
            res[key] /= N

    output = ''.join(('Скорость передачи: {speed:.3f} {type}it/sec\n',
                      'Скорость передачи, с учетом заголовков: {speed_headers:.3f} {type}it/sec\n',
                      'Общий объем: {received_volume:.3f} {type}\n',
                      'Общий объем, с учетом заголовков: {received_volume_headers:.3f} {type}\n',
                      'Общий объем TCP: {received_volume_tcp:.3f} {type}\n',
                      'Общий объем TCP, с учетом заголовков: {received_volume_tcp_headers:.3f} {type}\n',
                      'Общий объем UDP: {received_volume_udp:.3f} {type}\n',
                      'Общий объем UDP, с учетом заголовков: {received_volume_udp_headers:.3f} {type}\n',
                      'Время передачи: {full_time:.3f} sec\n',
                      '___________________________________________________')).format(
            speed=res['speed'],
            speed_headers=res['speed_headers'],
            received_volume=res['received_volume'],
            received_volume_headers=res['received_volume_headers'],
            received_volume_tcp=res['received_volume_tcp'],
            received_volume_tcp_headers=res['received_volume_tcp_headers'],
            received_volume_udp=res['received_volume_udp'],
            received_volume_udp_headers=res['received_volume_udp_headers'],
            full_time=res['full_time'],
            type=res['type']
        )

    print(output)


def calculate_result(tests):

    total_speed, total_time, total_speed_headers = [0] * 3

    for test in tests:
        total_speed += test['speed']
        total_time += test['full_time']
        total_speed_headers += test['speed_headers']

    res = {}

    # Лучшая скорость - минимальное время
    test = min(tests, key=itemgetter('full_time'))
    res['best_speed'] = test['speed']
    res['best_speed_headers'] = test['speed_headers']
    # Лучше время - максимальная скорость
    res['best_time'] = test['full_time']
    # Худшая скорость - максимальное время
    test = max(tests, key=itemgetter('full_time'))
    res['worst_speed'] = test['speed']
    res['worst_speed_headers'] = test['speed_headers']
    # Худшее время - минимальная скорость
    res['worst_time'] = test['full_time']
    # Средняя скорость
    res['average_speed'] = total_speed / len(tests)
    res['average_speed_headers'] = total_speed_headers / len(tests)
    # Среднее время
    res['average_time'] = total_time / len(tests)

    return res


def print_result(result):
    res = copy.deepcopy(result)
    idx_type = get_idx_type_data(res['best_speed'])
    res['type'] = TYPES_DATA[idx_type]
    N = 1024 ** idx_type

    res = copy.deepcopy(res)
    not_modifiable_by_key = ('worst_time', 'best_time', 'average_time', 'type')

    for key in res:
        if not(key in not_modifiable_by_key):
            res[key] /= N

    output = ''.join(('Худшая скорость: {worst_speed:.3f} {type}it/sec\n',
                      'Лучшая скорость: {best_speed:.3f} {type}it/sec\n',
                      'Средняя скорость: {average_speed:.3f} {type}it/sec\n',
                      'Худшая скорость, с учетом заголовков: {worst_speed_headers:.3f} {type}it/sec\n',
                      'Лучшая скорость, с учетом заголовков: {best_speed_headers:.3f} {type}it/sec\n',
                      'Средняя скорость, с учетом заголовков: {average_speed_headers:.3f} {type}it/sec\n',
                      'Худшее время: {worst_time:.3f} sec\n',
                      'Лучшее время: {best_time:.3f} sec\n',
                      'Среднее время: {average_time:.3f} sec')).format(
            worst_speed=res['worst_speed'],
            best_speed=res['best_speed'],
            average_speed=res['average_speed'],
            worst_speed_headers=res['worst_speed_headers'],
            best_speed_headers=res['best_speed_headers'],
            average_speed_headers=res['average_speed_headers'],
            worst_time=res['worst_time'],
            best_time=res['best_time'],
            average_time=res['average_time'],
            type=res['type']
        )
    print(output)


def server_mode(ports):
    """
    Сервер.
    """
    server = Server('0.0.0.0',
                    UDP_port=ports['UDP'],
                    TCP_port=ports['TCP'],
                    GetOptions_port=ports['GetOptions'])
    server.start()


def progress_bar(now_cycles, all_cycles, bar_size=40, symbol='|', space=' '):
    """
    Прогресс.
    Принимает количество циклов сейчас, количество циклов всего, размер прогресс-бара,
    символ-заполнитель прогресс-бара, символ-пустота прогресс-бара.
    """
    per = bar_size // all_cycles
    num_symbols = now_cycles * per
    num_space = bar_size - num_symbols
    # Откат каретки
    str_r = '\r' * (bar_size + 3)
    # Заполненный промежуток
    str_filled = symbol * num_symbols
    # Не заполненный промежуток
    str_not_filled = space * num_space
    return str_r + ' [' + str_filled + str_not_filled + ']'


def send_args_to_server(address, args, event_allow):
    """
    Отправить аргументы на сервер.
    Принимает адрес (IP & Port) сервера. аргументы для отправки
    и событие, которое скажет 2-му TCP-клиенту, что можно подключаться.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.connect(address)
    event_allow.set()
    # Отправка аргументов серверу
    s.send(pickle.dumps(args))
    s.close()


def csv_result_writer(data):
    """
    Запись результатов в CSV файл.
    Если файл отсутствует - создание и запись.
    Если файл существует - дозапись.
    """
    path = CSV_NAME
    is_write_header = not(os.path.exists(path) and os.path.isfile(path))
    with open(path, 'a', newline='') as csv_file:
        data['datetime'] = datetime.now().strftime('%d.%m.%Y %H:%M:%S')
        header = data.keys()
        writer = csv.DictWriter(csv_file, fieldnames=header)
        if is_write_header:
            writer.writeheader()
        writer.writerow(data)
        print(f"\nCSV файл '{path}' с результатами успешно создан/обновлен!")


def client_mode(cycles, ip_address, ports, args_to_server, is_csv):
    """
    Клиент.
    """
    print('Выполняется...')
    result_buffer = []
    for num_cycle in range(cycles):

        print(progress_bar(num_cycle, cycles, bar_size=60), end='')

        buffer = []
        sTCP = ClientTCP(
            address=(ip_address, ports['TCP']),
            buffer=buffer
        )
        sUDP = ClientUDP(
            address=('0.0.0.0', ports['UDP']),
            buffer=buffer
        )
        # Событие на разрешение подключения 2-ого TCP-клиента.
        # Первым подключается TCP-клиент для передачи аргументов серверу.
        # Вторым подключается 2-й TCP-клиент для тестов.
        event_allow = threading.Event()
        # Объявление и инициализация поток для связи с сервером
        threads = [
            # Передача аргументов от клиента к серверу
            ThreadEx(
                target=send_args_to_server,
                args=((ip_address, ports['GetOptions']),
                      args_to_server, event_allow),
                daemon=True
            ),
            # Запуск UDP-клиента a.k.a UDP-сервера для тестов
            ThreadEx(target=sUDP, daemon=True),
            # Подключение TCP-клиента для тестов
            ThreadEx(target=sTCP, args=(event_allow,), daemon=True)
        ]
        if args_to_server['ratio'] == 1:
            del threads[1]  # удаляем UDP, если передаются только TCP
        # Запуск потоков
        for thread in threads:
            thread.start()
        # Ожидание завершения потоков
        for thread in threads:
            thread.join_with_exception()
            thread.join()
        print(progress_bar(num_cycle, cycles, bar_size=60), end='')
        # Сохраняем результаты тестов
        res = calculate_test(buffer)
        result_buffer.append(res)
    print(progress_bar(cycles, cycles, bar_size=60), end='\n\n')
    sleep(1)
    # Вывод результатов
    total_result = calculate_result(result_buffer)
    for test in result_buffer:
        print_test(test)
    print_result(total_result)
    # Если нужна запись в CSV
    if is_csv:
        # Добавление конечных результатов по всем тестам
        csv_data = copy.deepcopy(total_result)
        # Добавление адреса
        csv_data['address'] = ip_address
        # Добавление портов
        for idx, key in enumerate(ports):
            csv_key = f'port_{idx + 1}'
            csv_data[csv_key] = ports[key]
        # Добавления аргументов посланых серверу
        for key in args_to_server:
            value = args_to_server[key]
            if key == 'volume':
                VALUE, TYPE = range(2)
                value = value[VALUE] * 1024 ** value[TYPE]
            csv_data[key] = value
        # Добавления количества циклов
        csv_data['cycles'] = cycles
        csv_result_writer(csv_data)


def main(args):
    """
    Точка входа в программу.
    """
    # Парсинг аргументов командной строки
    parser, namespace = parse(args)
    # Проверка аргументов порядок следования
    namespace = check_args(parser, namespace)
    # Определение портов для:
    # - передачи по TCP;
    # - передачи по UDP;
    # - передачи аргументов с клиента на сервер по TCP.
    ports = {
        'UDP': namespace.port,
        'TCP': namespace.port + 1,
        'GetOptions': namespace.port + 2
    }
    # Определение мода
    if namespace.server:
        server_mode(ports)
    elif namespace.client:
        args_to_server = {
            "volume": namespace.volume,
            "ratio": namespace.ratio,
            "orderby": namespace.orderby
        }
        # Получаем адрес хоста по имени
        address = socket.gethostbyname(namespace.address)
        try:
            client_mode(namespace.cycles, address, ports,
                        args_to_server, namespace.csv)
        except ConnectionRefusedError:
            print('\n\n[C] Подключение не установлено, т.к. конечный ПК отверг запрос на подключение!')
            sys.exit(1)

if __name__ == '__main__':
    try:
        main(sys.argv)
    except KeyboardInterrupt:
        sys.exit(0)
