import pymysql
import os
from datetime import datetime, timedelta
from processing_data import ProcessingData


class DataFetcher:
    __connection = None

    @staticmethod
    def get_connection():
        if DataFetcher.__connection is None:
            DataFetcher()
        return DataFetcher.__connection

    def __init__(self):
        """ Virtually private constructor. """
        if DataFetcher.__connection is not None:
            raise Exception("This class should only be created once.")
        else:
            host = os.getenv("DATAPLATTFORM_AURORA_HOST")
            db_name = os.getenv("DATAPLATTFORM_AURORA_DB_NAME")
            username = os.getenv("DATAPLATTFORM_AURORA_USER")
            password = os.getenv("DATAPLATTFORM_AURORA_PASSWORD")
            port = int(os.getenv("DATAPLATTFORM_AURORA_PORT"))

            connection = pymysql.connect(host=host,
                                         user=username,
                                         password=password,
                                         db=db_name,
                                         port=port,
                                         charset='utf8mb4',
                                         cursorclass=pymysql.cursors.DictCursor)
            DataFetcher.__connection = connection

    @staticmethod
    def fetch_data(date_from, days=1):
        """
        :param date_from: From where do you want to start fetching data.
        :param days: How many days of data do you want to fetch.
        :return: Returns both x_data and labels for all the `days` number of days.
        """
        date_to = date_from + timedelta(days=1)
        x_data_list = []
        label_list = []
        for _ in range(days):
            timestamp_from = date_from.timestamp()
            timestamp_to = date_to.timestamp()
            label = DataFetcher.fetch_label(timestamp_from, timestamp_to)

            if label is not None:
                x_data = DataFetcher.fetch_x_data(timestamp_from, timestamp_to)
                x_data_list.append(x_data)
                label_list.append(label)

            date_to += timedelta(days=1)
            date_from += timedelta(days=1)
        return x_data_list, label_list

    @staticmethod
    def fetch_x_data(timestamp_from, timestamp_to):
        """
        :param timestamp_from: unix timestamp.
        :param timestamp_to: unix timestamp.
        :return: All the features for a specific day.
        """
        results = {
            "weekday": DataFetcher.get_weekday(timestamp_to)
        }

        def execute_sql_and_process(processing_func, sql_query, only_one=False,
                                    sql_params=(timestamp_from, timestamp_to)):
            """
            :param processing_func: The processing function that should be run after the sql query.
            :param sql_query: The sql query string.
            :param only_one: Only fetch one record.
            :param sql_params: The parameters used to swap out %s in the sql query.
            :return: Nothing, this just updates the results dictionary with more keys and values.
            """
            cursor = DataFetcher.get_connection().cursor()
            cursor.execute(sql_query, sql_params)
            if only_one:
                query_result = cursor.fetchone()
            else:
                query_result = cursor.fetchall()
                if len(query_result) > 0 and "timestamp" in query_result[0]:
                    for i in range(len(query_result)):
                        time_of_day = DataFetcher.timestamp_to_time_of_day(
                            query_result[i]["timestamp"])
                        query_result[i]["time_of_day"] = time_of_day
                        del query_result[i]["timestamp"]
            query_result = processing_func(query_result)

            results.update(query_result)

        # TODO: Ideas for more features:
        #  * Which slack channel was talked in.
        #  * Weather
        #  * how many hours used on events this week. (This might be hard because they are quite
        #  delayed).
        #  * train and tram delays.

        # slack_sql = "SELECT COUNT(*) as `count`, `channel_name` FROM `SlackType` WHERE " \
        #             "`timestamp`>%s and `timestamp` <%s and `channel_name` IS NOT NULL GROUP" \
        #             " BY `channel_name` ORDER BY `count` desc"
        slack_sql = "SELECT `timestamp` FROM `SlackType` WHERE `timestamp`>%s AND `timestamp` <%s"
        execute_sql_and_process(ProcessingData.process_slack_data, slack_sql)

        slack_reactions_sql = "SELECT `reaction`, count(*) AS `count`, `positive_ratio`, " \
                              "`neutral_ratio`, `negative_ratio` FROM `SlackReactionType` WHERE " \
                              "`timestamp`>%s AND `timestamp` <%s AND `positive_ratio` IS " \
                              "NOT NULL GROUP BY `reaction`"
        execute_sql_and_process(ProcessingData.process_slack_reaction_data, slack_reactions_sql)

        github_sql = "SELECT COUNT(*) AS `count` FROM `GithubType` WHERE `timestamp`>%s AND " \
                     "`timestamp` <%s"
        execute_sql_and_process(ProcessingData.process_github_data, github_sql, only_one=True)

        # This is not grouping by each event. This query calculates the voting ratio of all the
        # events that happened today.
        event_rating_sql = "SELECT (sum(button) / count(button))*1000 AS `ratio` FROM " \
                           "`EventRatingType` WHERE `timestamp`>%s AND `timestamp`<%s"
        execute_sql_and_process(ProcessingData.process_event_rating_data, event_rating_sql,
                                only_one=True)

        # Because I want all the numbers as an int I multiply them by 10 and 100 here.
        # Before I later convert it to int.
        yr_sql = "SELECT 10 * (sum(`temperature`)/count(`temperature`)) AS `temp`, " \
                 "100 * (sum(`precipitation`)/count(`precipitation`)) AS `prec` FROM `YrType` " \
                 "WHERE `timestamp`>%s AND `timestamp`<%s"
        execute_sql_and_process(ProcessingData.process_weather_data, yr_sql, only_one=True)

        # Fetches the ratio between number of messages sent in negative channels against the
        # messages sent in every channel. (And multiply it by 1000 because we want it as an int
        # later.
        negative_slack_sql = """
            SELECT 1000 * (count(*) / `all`.`total_size`) AS `ratio`
            FROM `SlackType`
            JOIN (
                SELECT count(*) AS `total_size`
                FROM `SlackType`
                WHERE `timestamp`>%s AND `timestamp`<%s
            ) `all`
            WHERE (`channel_name` IN ("rant", "ubw") AND `timestamp`>%s AND `timestamp`<%s)"""
        execute_sql_and_process(ProcessingData.process_slack_negative_data, negative_slack_sql,
                                only_one=True, sql_params=(timestamp_from, timestamp_to) * 2)

        return results

    @staticmethod
    def fetch_label(timestamp_from, timestamp_to, skip_weekend=True):
        """
        :param timestamp_from: unix timestamp.
        :param timestamp_to: unix timestamp.
        :param skip_weekend: Should the weekend be skipped or not.
        :return: Either a number (float) between 0 and 1. or None if there was no ratings between
        the timestamps.
        """
        if skip_weekend:
            weekday = DataFetcher.get_weekday(timestamp_to)
            if weekday in [5, 6]:
                return None

        # Skip the current and future days.
        if datetime.now().timestamp() < timestamp_to:
            return None

        # In order to get the ratio in the range (0, 1) we add one, and divide by two.
        event_rating_sql = "select (((sum(button) / count(button)) + 1) / 2) as `ratio` from " \
                           "`DayRatingType` where `timestamp`>%s and `timestamp`<%s"
        cursor = DataFetcher.get_connection().cursor()
        cursor.execute(event_rating_sql, (timestamp_from, timestamp_to))
        query_result = cursor.fetchone()
        if query_result["ratio"] is not None:
            return float(query_result["ratio"])
        return None

    @staticmethod
    def timestamp_to_time_of_day(timestamp):
        """
        :param timestamp: unix timestamp.
        :return: Either 'early', 'midday' or 'late'.
        """
        dt_object = datetime.fromtimestamp(timestamp)
        hour = dt_object.hour
        if hour < 10:
            return "early"
        elif hour < 14:
            return "midday"
        else:
            return "late"

    @staticmethod
    def get_weekday(timestamp):
        """
        :param timestamp: unix timestamp.
        :return: a number in the range [0, 6]. 0 is monday etc..
        """
        dt_object = datetime.fromtimestamp(timestamp)
        return dt_object.weekday()
