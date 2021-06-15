# -*- coding: utf-8 -*-
# Main client file. Upon running asks input from the player
# and communicates with the server. clientHandler objects are
# created in serverMain
from threading import Thread, Lock, Condition
from clientIO import InputClosedException, SyncConsoleAppenderInputReader
from utils import enum, getmylogger
from socket import AF_INET, SOCK_STREAM, socket, SHUT_RD
from socket import error as soc_err
from messageProtocol import *


logging = getmylogger(__name__)


class Client:
    # Client can be in these states
    __gm_states = enum(
        NEED_NAME=0,
        NOTCONNECTED=1,
        SERVER_REFUSED_NAME=2,
        NEED_SESSION=3,
        WAIT_FOR_PLAYERS=4,
        NEED_PUTNUMBER=5,
    )
    # messages notified when client state changes
    __gm_ui_input_prompts = {
        __gm_states.NEED_NAME: "What's Your Nickname?",
        __gm_states.NOTCONNECTED: "What's The Server's IP Address?",
        __gm_states.SERVER_REFUSED_NAME: "That Name Is Already Taken",
        __gm_states.NEED_SESSION: """\nWant To [c]reate A New Session \
                                  Or [j]oin An Existing One?""",
        __gm_states.WAIT_FOR_PLAYERS: "Waiting For Other Players...",
        __gm_states.NEED_PUTNUMBER: """\nEnter column, row, number to \
                                    fill a spot.\n
                                    For example,\
                                    '213' puts '3' at (x=2, y=1).""",
    }

    def __init__(self, io):
        # Network related
        self.__send_lock = Lock()  # Only one entity can send out at a time
        self.__s = None
        # Here we collect the received responses and notify the waiting
        # entities
        self.__rcv_sync_msgs_lock = Condition()  # To wait/notify on received
        self.__rcv_sync_msgs = []  # To collect the received responses
        self.__rcv_async_msgs_lock = Condition()
        self.__rcv_async_msgs = []  # To collect the received notifications
        self.__io = io  # User interface IO
        # Current state of the game client
        self.__gm_state_lock = Lock()
        self.__gm_state = self.__gm_states.NEED_NAME
        # Stores the server approved name
        self.__my_name = None
        # Networking thread is created after the player has chose a name
        self.network_thread = None

    def __state_change(self, newstate):
        # Set the new state of the game and notifies the player
        with self.__gm_state_lock:
            self.__gm_state = newstate
            logging.debug("Game State Has Changed To {}".format(newstate))
            self.__io.output_sync(self.__gm_ui_input_prompts[newstate])

    def __sync_request(self, header, payload):
        # Send request and wait for response
        with self.__send_lock:
            req = header + HEADER_SEP + payload
            if self.__session_send(req):
                with self.__rcv_sync_msgs_lock:
                    while len(self.__rcv_sync_msgs) <= 0:
                        self.__rcv_sync_msgs_lock.wait()
                    rsp = self.__rcv_sync_msgs.pop()
                if rsp != "DIE!":
                    return rsp
            return None

    def __sync_response(self, rsp):
        # Collect the received response, notify waiting threads
        with self.__rcv_sync_msgs_lock:
            was_empty = len(self.__rcv_sync_msgs) <= 0
            self.__rcv_sync_msgs.append(rsp)
            if was_empty:
                self.__rcv_sync_msgs_lock.notifyAll()

    def __async_notification(self, msg):
        # Collect the received server notifications, notify waiting threads
        with self.__rcv_async_msgs_lock:
            was_empty = len(self.__rcv_async_msgs) <= 0
            self.__rcv_async_msgs.append(msg)
            if was_empty:
                self.__rcv_async_msgs_lock.notifyAll()

    def __session_rcv(self):
        # Receive the block of data till next block separator
        m, b = "", ""
        try:
            b = str(self.__s.recv(1), 'utf-8')
            m += b
            while len(b) > 0 and not (b.endswith(MSG_TERMCHR)):
                b = str(self.__s.recv(1), 'utf-8')
                m += b
            if len(b) <= 0:
                logging.debug("Socket Receive Interrupted")
                self.__s.close()
                m = ""
            m = m[:-1]
        except KeyboardInterrupt:
            self.__s.close()
            logging.info("Ctrl+C Issued, Terminating ...")
            m = ""
        except soc_err as e:
            if e.errno == 107:
                logging.warning("Server Closed Connection, Terminating ...")
            else:
                logging.error("Connection Error: {}".format(str(e)))
            self.__s.close()
            logging.info("Disconnected:(")
            m = ""
        return m

    def __session_send(self, msg):
        # Sends the data with message end char
        m = msg + MSG_TERMCHR
        r = False
        try:
            self.__s.sendall(m.encode('utf-8'))
            r = True
        except KeyboardInterrupt:
            self.__s.close()
            logging.info("Ctrl+C Issued, Terminating...")
        except soc_err as e:
            if e.errno == 107:
                logging.warning("Server Closed Connection, Terminating...")
            else:
                logging.error("Connection Error: {}".format(str(e)))
            self.__s.close()
            logging.info("Disconnected:(")
        return r

    def __protocol_rcv(self, message):
        # Process Received Messages.
        # Server notifications, request/responses and game end
        # messages are processed separately
        logging.debug("Received [{} bytes] in total".format(len(message)))
        if len(message) < 2:
            logging.debug("Not Enough Data Received From {}".format(message))
            return
        logging.debug("Response Control Code [{}]".format(message[0]))
        if message.startswith(REP_NOTIFY + HEADER_SEP):
            payload = message[2:]
            logging.debug("Server Notification Received: {}".format(payload))
            self.__async_notification(payload)
        elif message[:2] in map(
            lambda x: x + HEADER_SEP,
            [
                REP_CURRENT_SESSIONS,
                REP_PUT_NR,
                REP_WAITING_PLAYERS,
                REP_TABLE,
                REP_NOT_OK,
            ],
        ):
            self.__sync_response(message)
        elif message[:2] == REP_SCORES_GAME_OVER + HEADER_SEP:
            self.__async_notification("The Game Has Ended. {}\n".format(message[2:]))
            self.__state_change(self.__gm_states.NEED_SESSION)
        else:
            logging.debug("Unknown Control Message Received: {}".format(message))
            return REP_NOT_OK

    def __get_user_input(self):
        # Gather User Input
        try:
            msg = self.__io.input_sync()
            logging.debug("User Entered: {}".format(msg))
            return msg
        except InputClosedException:
            return None

    def set_user_name(self, user_input):
        # Locally sets the players name, asks server for verification
        is_suitable = True
        if len(user_input) not in range(1, 9):
            for c in user_input:
                if not c.isalnum():
                    is_suitable = False
                    break
        if not is_suitable:
            self.__io.output_sync("Not A Suitable Name, Try Again!!")
        else:
            self.__my_name = user_input
            if self.__gm_state == self.__gm_states.NEED_NAME:
                self.__state_change(self.__gm_states.NOTCONNECTED)
            elif self.__gm_state == self.__gm_states.SERVER_REFUSED_NAME:
                self.send_server_my_name_get_ack()

    def send_server_my_name_get_ack(self):
        # Ask server for name verification
        try:
            rsp = self.__sync_request(REQ_NICKNAME, self.__my_name)
            header, msg = rsp.split(HEADER_SEP)
            if header == REP_NOT_OK:
                self.__state_change(self.__gm_states.SERVER_REFUSED_NAME)
            elif header == REP_CURRENT_SESSIONS:
                self.__state_change(self.__gm_states.NEED_SESSION)
        except Exception as e:
            self.__io.output_sync(
                "Name verification by client failed {}".format(str(e))
            )

    def get_connected(self, ip):
        # Connects to the server, creates networking thread,
        # calls name verification
        self.__s = socket(AF_INET, SOCK_STREAM)
        server_addr = (ip, 7777)
        try:
            self.__s.connect(server_addr)
            logging.info(
                "Connected to Game server at {}:{}".format(
                    server_addr[0], server_addr[1]
                )
            )
            self.network_thread = Thread(name="NetworkThread", target=self.network_loop)
            self.network_thread.start()
            self.send_server_my_name_get_ack()
        except soc_err as e:
            logging.error(
                "Can not connect to game server at %s:%d"
                " %s " % (server_addr + (str(e),))
            )
            self.__io.output_sync("Can't connect to server!")

    def get_session(self, create_sess):
        # Asks the player if to create a new session or join an existing one
        # guides the player through the process while checking if the input is
        # valid. Then creates/connects to the session.
        while create_sess not in ["c", "j"]:
            try:
                create_sess = self.__get_user_input()
            except KeyboardInterrupt:
                return False
            if create_sess == "Q":
                return False
            if create_sess not in ["c", "j"]:
                self.__io.output_sync("Error, Enter Either 'c' or 'j'.")
        p_count = "0"
        if create_sess == "c":
            while int(p_count) < 2:
                self.__io.output_sync("How many people are playing?")
                try:
                    p_count = self.__get_user_input()
                    if p_count == "Q":
                        return False
                    if int(p_count) < 2:
                        self.__io.output_sync("Need a minimum of two players!")
                except KeyboardInterrupt:
                    return False
                except ValueError:
                    self.__io.output_sync("Please enter a number.")
        self.__io.output_sync("What's the session's name?")
        valid_name = False
        sess_name = ""
        while not valid_name:
            valid_name = True
            try:
                sess_name = self.__get_user_input()
            except KeyboardInterrupt:
                return False
            if sess_name == "Q":
                return False
            if len(sess_name) not in range(1, 9):
                self.__io.output_sync("Length of the name much be in [1..9].")
                valid_name = False
            else:
                for c in sess_name:
                    if not c.isalnum():
                        self.__io.output_sync(
                            "Session name contains illegal characters."
                        )
                        valid_name = False
                        break

        if create_sess == "j":
            rsp = self.__sync_request(REQ_JOIN_EXIST_SESS, sess_name)
        else:
            rsp = self.__sync_request(
                REQ_JOIN_NEW_SESS, sess_name + FIELD_SEP + p_count
            )
        try:
            header, msg = rsp.split(HEADER_SEP)
            if header == REP_NOT_OK:
                self.__io.output_sync("Error Joining Session: {}".format(msg))
            elif header == REP_WAITING_PLAYERS:
                self.__state_change(self.__gm_states.WAIT_FOR_PLAYERS)
            elif header == REP_TABLE:
                self.__io.output_sync(">>> Game Started! \n\n{}".format(rsp[2:]))
                self.__state_change(self.__gm_states.NEED_PUTNUMBER)
        except Exception as e:
            self.__io.output_sync(
                "Analysing sess join/create msg fail {}".format(str(e))
            )
        return True

    def waiting_for_players(self):
        # loop till the server notifies all the clients have connected
        # to the game session
        with self.__rcv_sync_msgs_lock:
            while len(self.__rcv_sync_msgs) <= 0:
                try:
                    self.__rcv_sync_msgs_lock.wait(0.1)
                except KeyboardInterrupt:
                    return False
                except RuntimeError:
                    continue
            rsp = self.__rcv_sync_msgs.pop()
        if rsp.startswith(REP_TABLE + HEADER_SEP):
            self.__io.output_sync(">>> Game Started! \n\n{}".format(rsp[2:]))
            self.__state_change(self.__gm_states.NEED_PUTNUMBER)
        return True

    def putNumber(self, s):
        # interacts with server's Sudoku board. Checks if client has
        # input correctly three numbers in range 1...9
        if len(s) != 3:
            self.__io.output_sync("Not proper input - did not give three " "integers")
            return
        try:
            ints = list(s)
            for nr in [int(ints[0]), int(ints[1]), int(ints[2])]:
                if nr not in range(1, 10):
                    self.__io.output_sync("Not proper input - numbers not " "1...9")
                    return
        except ValueError:
            self.__io.output_sync("Not proper input - cant find three integers")
            rsp = self.__sync_request(REQ_PUT_NR, s)
            if rsp.startswith(REP_PUT_NR + HEADER_SEP):
                self.__io.output_sync("{}".format(rsp[2:]))
            else:
                self.__io.output_sync("Incorrect server response: ({})".format(rsp))

    def stop(self):
        # Stop the game client (it's socket and notification thread)
        if self.__s is not None:
            try:
                self.__s.shutdown(SHUT_RD)
            except soc_err:
                logging.warning("The Socket Was not connected anyway ..")
            finally:
                self.__s.close()
        self.__sync_response("DIE!")
        self.__async_notification("DIE!")

    def game_loop(self):
        # Main game loop (notifications-loop are running already)
        # Networking thread gets started along the way
        self.__io.output_sync("\nPress Enter ⮐ to initiate input.")
        self.__io.output_sync("What's Your NickName?")
        while True:
            if self.__gm_state != self.__gm_states.WAIT_FOR_PLAYERS:
                user_input = self.__get_user_input()
            if user_input == "Q":
                break
            elif (
                self.__gm_state == self.__gm_states.NEED_NAME
                or self.__gm_state == self.__gm_states.SERVER_REFUSED_NAME
            ):
                self.set_user_name(user_input)
            elif self.__gm_state == self.__gm_states.NOTCONNECTED:
                self.get_connected(user_input)
            elif self.__gm_state == self.__gm_states.NEED_SESSION:
                if not self.get_session(user_input):
                    break
            elif self.__gm_state == self.__gm_states.WAIT_FOR_PLAYERS:
                if not self.waiting_for_players():
                    break
            elif self.__gm_state == self.__gm_states.NEED_PUTNUMBER:
                self.putNumber(user_input)
        self.__io.output_sync("Queue Entered, Disconnecting...")

    def notifications_loop(self):
        # Iterate over received notifications, show them to user, wait if
        # no notifications
        logging.info("Falling To notifier loop ...")
        while True:
            with self.__rcv_async_msgs_lock:
                if len(self.__rcv_async_msgs) <= 0:
                    self.__rcv_async_msgs_lock.wait()
                msg = self.__rcv_async_msgs.pop(0)
                if msg == "DIE!":
                    return
            self.__io.output_sync(msg)

    def network_loop(self):
        # Network Receiver/Message Processor loop. Stops if empty char
        # has been received
        logging.info("Falling To Receiver Loop ...")
        while True:
            m = self.__session_rcv()
            if len(m) <= 0:
                break
            self.__protocol_rcv(m)


if __name__ == "__main__":
    print("Starting Client Application...")
    sync_io = SyncConsoleAppenderInputReader()
    client = Client(sync_io)
    notifications_thread = Thread(
        name="NotificationsThread", target=client.notifications_loop
    )
    notifications_thread.start()
    try:
        client.game_loop()
    except KeyboardInterrupt:
        logging.warning("Ctrl+C Issued, Terminating ...")
    finally:
        client.stop()
    if client.network_thread is not None:
        client.network_thread.join()
    notifications_thread.join()
    logging.info("Terminating")
